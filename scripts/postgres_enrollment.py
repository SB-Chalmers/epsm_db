"""Password activation app. Run behind an HTTPS proxy with Gunicorn, never as root."""
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from flask import Flask, jsonify, request
import psycopg

try:
    from .db_access_common import cid,token_hash,connection_details,email_message,deliver
except ImportError:  # Installed together under /opt/ubem-enrollment.
    from db_access_common import cid,token_hash,connection_details,email_message,deliver

PAGE='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Chalmers UBEM database access</title><link rel="stylesheet" href="/style.css">
<main><h1>Choose your database password</h1><p>Use the Chalmers network or VPN. This password is for the UBEM database.</p>
<form id="setup"><label>Chalmers CID<input id="cid" readonly required></label>
<label>New password<input id="password" type="password" autocomplete="new-password" minlength="12" required></label>
<label>Repeat password<input id="confirm" type="password" autocomplete="new-password" minlength="12" required></label>
<p>Use at least 12 characters. Your password will not be sent by email.</p><button>Activate database access</button></form>
<p id="status" role="status" aria-live="polite"></p><pre id="details"></pre>
<p>Support: <a id="support"></a></p></main><script src="/setup.js" defer></script></html>'''
JS='''const values=new URLSearchParams(location.hash.slice(1));
const token=values.get('token')||'';
document.querySelector('#cid').value=values.get('cid')||'';
history.replaceState(null,'',location.pathname);
const form=document.querySelector('#setup'),status=document.querySelector('#status');
fetch('/info').then(r=>r.json()).then(info=>{const a=document.querySelector('#support');a.textContent=info.support;a.href='mailto:'+info.support;});
if(!token){status.textContent='Open the full setup link from your invitation email.';form.hidden=true;}
form.addEventListener('submit',async event=>{
 event.preventDefault();const button=form.querySelector('button');button.disabled=true;
 status.textContent='Activating your database account…';
 try{const response=await fetch('/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:document.querySelector('#cid').value,token,password:document.querySelector('#password').value,confirm:document.querySelector('#confirm').value})});
 const result=await response.json();status.textContent=result.message;
 if(response.ok){form.reset();form.hidden=true;document.querySelector('#details').textContent=Object.entries(result.connection).map(([k,v])=>k+': '+v).join('\\n')+'\\nPassword: the password you just chose';}
 }catch(error){status.textContent='Connection failed. Check your Chalmers network/VPN and try again.';}
 finally{button.disabled=false;}
});'''
CSS='''body{font:17px/1.5 system-ui,sans-serif;margin:0;background:#f4f6f8;color:#172235}main{max-width:620px;margin:5vh auto;padding:32px;background:white;border-radius:12px}label{display:block;margin:18px 0}input{box-sizing:border-box;display:block;width:100%;font:inherit;padding:10px}button{font:inherit;padding:12px 18px;cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}button:disabled{opacity:.6}a{color:#174ea6}'''


def activate(config,name,token,password):
    # The service role has only EXECUTE on the constrained activation function.
    with psycopg.connect(config['service_database_url'],connect_timeout=5,
                         options='-c statement_timeout=10000') as connection:
        verifier=connection.pgconn.encrypt_password(password.encode(),name.encode(),b'scram-sha-256').decode()
        connection.execute('SELECT ubem_access.activate(%s,%s,%s)',(name,token_hash(token),verifier)).fetchone()
    return connection_details(config,name)


def create_app(config=None):
    if config is None:
        config=json.loads(Path(os.environ.get('UBEM_ACCESS_CONFIG','/etc/ubem-access/service.json')).read_text())
    app=Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=8192,TRUSTED_HOSTS=[urlsplit(config['activation_origin']).hostname])

    @app.after_request
    def security(response):
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Strict-Transport-Security']='max-age=31536000'
        response.headers['Content-Security-Policy']="default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        return response

    @app.get('/')
    def index():
        if config.get('transport')=='tailscale':
            return PAGE.replace('Use the Chalmers network or VPN.', 'Keep Tailscale connected to access this database laptop.')
        return PAGE

    @app.get('/setup.js')
    def javascript():
        return app.response_class(JS.replace('Chalmers network/VPN', 'Tailscale connection') if config.get('transport')=='tailscale' else JS,mimetype='application/javascript')

    @app.get('/style.css')
    def stylesheet():
        return app.response_class(CSS,mimetype='text/css')

    @app.get('/info')
    def info():
        return jsonify(support=config['admin_email'])

    @app.get('/health')
    def health():
        return jsonify(service='ubem-postgres-enrollment',status='ok')

    @app.post('/activate')
    def enroll():
        if request.headers.get('Origin') not in (None,config['activation_origin']):
            return jsonify(message='Open the setup page from your invitation email.'),403
        if not request.is_json:
            return jsonify(message='A JSON request is required.'),415
        data=request.get_json(silent=True)
        try:
            if not isinstance(data,dict):
                raise ValueError('Invalid request.')
            name=cid(data.get('cid',''))
            token=data.get('token','')
            password=data.get('password','')
            if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
                raise ValueError('Invalid invitation link. Contact the administrator for a new one.')
            if not isinstance(password,str) or not 12<=len(password)<=256 or '\x00' in password:
                raise ValueError('Use a password between 12 and 256 characters.')
            if password!=data.get('confirm'):
                raise ValueError('The passwords do not match.')
            details=activate(config,name,token,password)
        except (ValueError,TypeError,AttributeError) as error:
            return jsonify(message=str(error)),400
        except psycopg.Error:
            # Never log SQL parameter values, invitation tokens, or passwords.
            return jsonify(message='Invitation invalid, expired, or already used. Contact the administrator.'),400
        try:
            queued,_=deliver(config,email_message(config,name),
                             config.get('outbox','/var/lib/ubem-enrollment/outbox'),local_smtp=True)
        except OSError:
            queued=False
        message='Your database account is active. Connection details are below.'
        message+=(' A confirmation email has been queued.' if queued else
                  ' The confirmation email could not be queued; keep these details and contact support.')
        return jsonify(message=message,connection=details)
    return app
