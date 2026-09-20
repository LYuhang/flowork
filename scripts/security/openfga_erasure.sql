\set ON_ERROR_STOP on

-- The maintenance identity can execute one narrowly scoped SECURITY DEFINER
-- function. It receives no table privileges and cannot inspect or modify live
-- OpenFGA relationship tuples.
SELECT format(
  'CREATE ROLE flowork_openfga_erasure LOGIN PASSWORD %L',
  :'erasure_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles
  WHERE rolname = 'flowork_openfga_erasure'
) \gexec

SELECT format(
  'ALTER ROLE flowork_openfga_erasure PASSWORD %L',
  :'erasure_password'
) \gexec

CREATE OR REPLACE FUNCTION public.flowork_erase_changelog(
  p_store text,
  p_subjects text[],
  p_object_ids text[]
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
  removed bigint;
BEGIN
  DELETE FROM public.changelog
  WHERE store = p_store
    AND (
      _user = ANY (p_subjects)
      OR object_id = ANY (p_object_ids)
    );
  GET DIAGNOSTICS removed = ROW_COUNT;
  RETURN removed;
END
$function$;

REVOKE ALL ON FUNCTION public.flowork_erase_changelog(
  text,
  text[],
  text[]
) FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM flowork_openfga_erasure;
SELECT format(
  'GRANT CONNECT ON DATABASE %I TO flowork_openfga_erasure',
  :'erasure_database'
) \gexec
GRANT USAGE ON SCHEMA public TO flowork_openfga_erasure;
GRANT EXECUTE ON FUNCTION public.flowork_erase_changelog(
  text,
  text[],
  text[]
) TO flowork_openfga_erasure;
