import os
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


def connect(chat=False, autocommit=False):
    url = os.getenv('CHAT_DATABASE_URL' if chat else 'DATABASE_URL')
    if url:
        return psycopg.connect(url, autocommit=autocommit, connect_timeout=10)
    return psycopg.connect(host=os.getenv('POSTGRES_HOST', '127.0.0.1'),
        port=os.getenv('POSTGRES_PORT', '5434'), user=os.getenv('POSTGRES_USER', '40kbot'),
        password=os.getenv('POSTGRES_PASSWORD'),
        dbname=os.getenv('CHAT_POSTGRES_DB', 'lexi_chat') if chat else os.getenv('POSTGRES_DB', '40kbot'),
        autocommit=autocommit, connect_timeout=10)


def initialize():
    if not os.getenv('CHAT_DATABASE_URL'):
        name = os.getenv('CHAT_POSTGRES_DB', 'lexi_chat')
        with connect(autocommit=True) as conn:
            if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone():
                conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    with connect(chat=True) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS user_settings (
            user_id text PRIMARY KEY, personality text NOT NULL DEFAULT 'lexi'
                CHECK(personality IN ('lexi','neutral')),
            custom_prompt text NOT NULL DEFAULT '', faction text NOT NULL DEFAULT '',
            detachment text NOT NULL DEFAULT '', updated_at timestamptz NOT NULL DEFAULT now())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS conversations (
            conversation_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now())''')
        conn.execute('''ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS user_id text,
            ADD COLUMN IF NOT EXISTS guild_id text,
            ADD COLUMN IF NOT EXISTS channel_id text''')
        conn.execute('''UPDATE conversations SET
            user_id=split_part(conversation_id, ':', 3),
            guild_id=NULLIF(split_part(conversation_id, ':', 1), 'dm'),
            channel_id=split_part(conversation_id, ':', 2)
            WHERE conversation_id ~ '^(dm|[0-9]+):[0-9]+:[0-9]+$'
                AND (user_id IS NULL OR channel_id IS NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS messages (
            message_id text PRIMARY KEY, conversation_id text REFERENCES conversations ON DELETE CASCADE,
            author_id text NOT NULL, role text NOT NULL CHECK(role IN ('user','assistant')),
            content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
        conn.execute('CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id,created_at)')
        conn.execute('''CREATE TABLE IF NOT EXISTS tool_calls (
            id bigserial PRIMARY KEY, conversation_id text REFERENCES conversations ON DELETE CASCADE,
            message_id text, tool_name text NOT NULL, arguments jsonb NOT NULL,
            result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
        conn.execute('''CREATE OR REPLACE VIEW debug_timeline AS
            WITH events AS (
                SELECT m.created_at AS event_time, m.conversation_id,
                    'message:' || m.message_id AS event_id,
                    CASE WHEN m.role='user' THEN 'question' ELSE 'answer' END AS event_type,
                    m.message_id, CASE WHEN m.role='user' THEN m.message_id END AS question_message_id,
                    m.author_id, m.content AS text, NULL::text AS tool_name,
                    NULL::jsonb AS arguments, NULL::jsonb AS result,
                    CASE WHEN m.role='user' THEN 0 ELSE 2 END AS sort_order,
                    NULL::bigint AS tool_id
                FROM messages m
                UNION ALL
                SELECT t.created_at, t.conversation_id, 'tool:' || t.id, 'tool',
                    NULL, t.message_id, q.author_id,
                    COALESCE(t.arguments->>'query', t.arguments->>'entity_id', t.tool_name),
                    t.tool_name, t.arguments, t.result, 1, t.id
                FROM tool_calls t
                LEFT JOIN messages q ON q.message_id=t.message_id
                    AND q.conversation_id=t.conversation_id AND q.role='user'
            )
            SELECT row_number() OVER (ORDER BY event_time, sort_order, tool_id, event_id) AS step,
                event_time, events.conversation_id, event_type, text, tool_name,
                COALESCE((SELECT jsonb_agg(DISTINCT u.value ORDER BY u.value)
                    FROM (
                        SELECT value FROM jsonb_path_query(events.result, '$.**.source_url') AS paths(value)
                        UNION ALL SELECT value FROM jsonb_path_query(events.result, '$.**.url') AS paths(value)
                        UNION ALL SELECT value FROM jsonb_path_query(events.result, '$.**.link') AS paths(value)
                        UNION ALL SELECT value FROM jsonb_path_query(events.result, '$.**.source_urls[*]') AS paths(value)
                        UNION ALL SELECT value FROM jsonb_path_query(events.result, '$.**.errata_link') AS paths(value)
                    ) u WHERE jsonb_typeof(u.value)='string'
                    AND (u.value #>> '{}') ~ '^https?://'), '[]'::jsonb) AS source_urls,
                arguments, result, message_id, question_message_id, author_id, event_id,
                c.user_id, c.guild_id, c.channel_id
            FROM events LEFT JOIN conversations c ON c.conversation_id=events.conversation_id''')


def save_message(conversation, message_id, author, role, content):
    with connect(chat=True) as conn:
        parts = conversation.split(':')
        valid = len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit() and (parts[0] == 'dm' or parts[0].isdigit())
        user_id, guild_id, channel_id = (parts[2], None if parts[0] == 'dm' else parts[0], parts[1]) if valid else (None, None, None)
        conn.execute('''INSERT INTO conversations(conversation_id,user_id,guild_id,channel_id)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (conversation, user_id, guild_id, channel_id))
        return conn.execute('''INSERT INTO messages(message_id,conversation_id,author_id,role,content)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING message_id''',
            (str(message_id), conversation, str(author), role, content)).fetchone() is not None


def history(conversation):
    with connect(chat=True) as conn:
        rows = conn.execute('''SELECT role,content FROM messages WHERE conversation_id=%s
            ORDER BY created_at DESC,message_id DESC LIMIT 20''', (conversation,)).fetchall()
    return [{'role': role, 'content': content} for role, content in reversed(rows)]


def log_tool(conversation, message_id, name, arguments, result):
    with connect(chat=True) as conn:
        conn.execute('''INSERT INTO tool_calls(conversation_id,message_id,tool_name,arguments,result)
            VALUES(%s,%s,%s,%s,%s)''', (conversation, str(message_id), name, Jsonb(arguments), Jsonb(result)))


def clear(conversation):
    with connect(chat=True) as conn:
        conn.execute('DELETE FROM conversations WHERE conversation_id=%s', (conversation,))


def delete_message(message_id):
    with connect(chat=True) as conn:
        conn.execute('DELETE FROM messages WHERE message_id=%s', (str(message_id),))
        conn.execute('DELETE FROM tool_calls WHERE message_id=%s', (str(message_id),))


def prune():
    days = int(os.getenv('CHAT_RETENTION_DAYS', '30'))
    if days < 1:
        raise ValueError('CHAT_RETENTION_DAYS must be positive')
    with connect(chat=True) as conn:
        for table in ('messages', 'tool_calls'):
            conn.execute(sql.SQL("DELETE FROM {} WHERE created_at < now() - %s * interval '1 day'").format(sql.Identifier(table)), (days,))


def preferences(user_id):
    with connect(chat=True) as conn:
        row=conn.execute('SELECT personality,custom_prompt,faction,detachment FROM user_settings WHERE user_id=%s',(str(user_id),)).fetchone()
    return dict(zip(('personality','custom_prompt','faction','detachment'),row or ('lexi','','','')))


def set_preferences(user_id, **changes):
    limits={'personality':7,'custom_prompt':1500,'faction':100,'detachment':150}
    if not changes or any(k not in limits or not isinstance(v,str) or len(v)>limits[k] for k,v in changes.items()):
        raise ValueError('Invalid preference')
    if 'personality' in changes and changes['personality'] not in {'lexi','neutral'}:
        raise ValueError('Invalid personality')
    with connect(chat=True) as conn:
        conn.execute('INSERT INTO user_settings(user_id) VALUES(%s) ON CONFLICT DO NOTHING',(str(user_id),))
        assignments=sql.SQL(', ').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in changes)
        conn.execute(sql.SQL('UPDATE user_settings SET {},updated_at=now() WHERE user_id=%s').format(assignments),[*changes.values(),str(user_id)])
    return preferences(user_id)


def latest_trace(conversation, question_id=None):
    with connect(chat=True) as conn:
        if question_id:
            question=conn.execute("SELECT message_id,content,created_at FROM messages WHERE message_id=%s AND role='user'",(question_id,)).fetchone()
            if question:
                conversation=conn.execute('SELECT conversation_id FROM messages WHERE message_id=%s',(question_id,)).fetchone()[0]
        else:
            question=conn.execute("SELECT message_id,content,created_at FROM messages WHERE conversation_id=%s AND role='user' ORDER BY created_at DESC LIMIT 1",(conversation,)).fetchone()
        if not question:
            return None
        tools=conn.execute('SELECT tool_name,arguments,result,created_at FROM tool_calls WHERE conversation_id=%s AND message_id=%s ORDER BY created_at,id',(conversation,question[0])).fetchall()
        answers=conn.execute('''SELECT content FROM messages WHERE conversation_id=%s AND role='assistant'
            AND created_at >= %s AND created_at < COALESCE((SELECT min(created_at) FROM messages
                WHERE conversation_id=%s AND role='user' AND created_at>%s),'infinity'::timestamptz)
            ORDER BY created_at,message_id''',(conversation,question[2],conversation,question[2])).fetchall()
    return {'question_message_id':question[0],'conversation_id':conversation,'question':question[1],
        'answer':'\n'.join(r[0] for r in answers),'tools':[{'name':r[0],'arguments':r[1],'result':r[2],'completed_at':r[3].isoformat()} for r in tools]}
