"""Saved, inspectable selection rules. No user SQL is stored or executed."""

from psycopg.types.json import Jsonb
from .database import _connect


def validate_scope(rules):
    if not isinstance(rules, dict) or set(rules) - {"purposes", "include_support", "existing_only"}:
        raise ValueError("Supported scope fields: purposes, include_support, existing_only")
    if "purposes" in rules and (
        not isinstance(rules["purposes"], list)
        or any(not isinstance(x, str) or not x for x in rules["purposes"])
    ):
        raise ValueError("purposes must be a list of nonempty source labels")
    for field in ("include_support", "existing_only"):
        if field in rules and not isinstance(rules[field], bool):
            raise ValueError(f"{field} must be boolean")


def evaluate_scopes(url, dataset, scope_id="all-buildings", rules=None):
    rules = rules if rules is not None else {"include_support": False, "existing_only": False}
    validate_scope(rules)
    with _connect(url) as c:
        c.execute(
            "INSERT INTO scope.definition(dataset_id,scope_id,rules) VALUES(%s,%s,%s) ON CONFLICT(dataset_id,scope_id) DO UPDATE SET rules=EXCLUDED.rules",
            (dataset, scope_id, Jsonb(rules)),
        )
        c.execute(
            "DELETE FROM scope.membership WHERE dataset_id=%s AND scope_id=%s", (dataset, scope_id)
        )
        c.execute(
            """INSERT INTO scope.membership(dataset_id,scope_id,building_id,inclusion_state,reasons)
          SELECT dataset_id,%s,building_id,CASE WHEN cardinality(reasons)=0 THEN 'included' ELSE 'excluded' END,
          CASE WHEN cardinality(reasons)=0 THEN ARRAY['matches_rules'] ELSE reasons END
          FROM (SELECT f.dataset_id,f.building_id,array_remove(ARRAY[
            CASE WHEN f.selection_role='epc_support' AND NOT %s THEN 'supporting_building' END,
            CASE WHEN %s AND f.is_existing IS DISTINCT FROM true THEN 'not_verified_existing' END,
            CASE WHEN cardinality(%s::text[])>0 AND NOT f.purposes && %s::text[] THEN 'purpose_outside_scope' END],NULL) reasons
            FROM mart.building_facts f WHERE f.dataset_id=%s) x""",
            (
                scope_id,
                rules.get("include_support", False),
                rules.get("existing_only", False),
                rules.get("purposes", []),
                rules.get("purposes", []),
                dataset,
            ),
        )
    return {"dataset": dataset, "scope": scope_id, "rules": rules}
