"""Wake a user's attention menus after committed request changes."""
from alembic import op

revision = '175'
down_revision = '174'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE FUNCTION notify_chat_attention() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE owner_key text;
    BEGIN
      IF TG_TABLE_NAME = 'chats' THEN
        owner_key := COALESCE(NEW.tenant_id, OLD.tenant_id)::text || ':' ||
                     COALESCE(NEW.creator_user_id, OLD.creator_user_id)::text;
      ELSE
        SELECT tenant_id::text || ':' || creator_user_id::text INTO owner_key
          FROM chats WHERE chat_id = COALESCE(NEW.chat_id, OLD.chat_id);
      END IF;
      IF owner_key IS NOT NULL THEN
        PERFORM pg_notify('flowork_chat_attention', owner_key);
      END IF;
      RETURN COALESCE(NEW, OLD);
    END; $$""")
    op.execute("""CREATE TRIGGER chat_attention_changed
        AFTER INSERT OR UPDATE OR DELETE ON hitl_requests
        FOR EACH ROW EXECUTE FUNCTION notify_chat_attention()""")
    op.execute("""CREATE TRIGGER chat_attention_removed
        AFTER UPDATE OF deleted_at OR DELETE ON chats
        FOR EACH ROW EXECUTE FUNCTION notify_chat_attention()""")


def downgrade():
    op.execute('DROP TRIGGER chat_attention_removed ON chats')
    op.execute('DROP TRIGGER chat_attention_changed ON hitl_requests')
    op.execute('DROP FUNCTION notify_chat_attention()')
