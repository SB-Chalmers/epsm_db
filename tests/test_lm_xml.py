import xml.etree.ElementTree as ET

from epsm_db.lm_xml import parse_address_element, parse_building_element


def test_building_xml_preserves_repeated_purposes_and_entrances():
    root = ET.fromstring("""<Byggnad xmlns='urn:lm'><objektidentitet>b1</objektidentitet>
      <objektstatus>Gällande</objektstatus><Byggnadsattribut><objektversion>2</objektversion>
      <versionGiltigFran>2025-01-01T00:00:00+01:00</versionGiltigFran><husnummer>1</husnummer>
      <nybyggnadsar>1980</nybyggnadsar><huvudbyggnad>true</huvudbyggnad></Byggnadsattribut>
      <Bostad><huvudandamal>true</huvudandamal><andamal>Flerfamiljshus</andamal></Bostad>
      <Verksamhet><huvudandamal>false</huvudandamal><andamal>Kontor</andamal></Verksamhet>
      <Entre><objektidentitet>e1</objektidentitet><adressplats>a1</adressplats></Entre>
      <Entre><objektidentitet>e2</objektidentitet><adressplats>a2</adressplats></Entre>
    </Byggnad>""")
    row = parse_building_element(root, source_file="fixture.xml", source_record=7)
    assert [item["category"] for item in row["purposes"]] == ["Bostad", "Verksamhet"]
    assert [item["address_place_id"] for item in row["entrances"]] == ["a1", "a2"]
    assert row["construction_year"] == 1980


def test_address_gml_axis_order_is_northing_easting():
    root = ET.fromstring("""<Belagenhetsadress xmlns='urn:lm' xmlns:gml='http://www.opengis.net/gml/3.2'>
      <objektidentitet>a1</objektidentitet><Adressplatsattribut><objektversion>1</objektversion>
      <Adressplatsbeteckning><adressplatsnummer>12</adressplatsnummer></Adressplatsbeteckning>
      <Adressplatspunkt><gml:Point srsName='urn:ogc:def:crs:EPSG::3006'><gml:pos>6400000 320000</gml:pos></gml:Point></Adressplatspunkt>
      <postort>Göteborg</postort></Adressplatsattribut><Adressomrade><faststalltNamn>Testgatan</faststalltNamn>
      <Kommundel><Kommun><kommunkod>1480</kommunkod></Kommun></Kommundel></Adressomrade>
    </Belagenhetsadress>""")
    row = parse_address_element(root, source_file="fixture.xml", source_record=1)
    assert (row["easting"], row["northing"]) == (320000.0, 6400000.0)
