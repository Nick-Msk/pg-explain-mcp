-- Configuration schema for pg-explain-mcp.
-- Loaded by `python -m pg_explain_mcp.config --init`.

-- Configuration schema for pg-explain-mcp.
-- Loaded by `python -m pg_explain_mcp.config --init`.

create table if not exists databases (
    database    text    primary key
);

create table if not exists checks (
    num         integer not null,
    database    text    not null,
    name        text    not null,
    description text    not null,
    enabled     integer not null default 1,
    primary key (num, database),
    foreign key (database) references databases (database)
        on delete cascade
);

create unique index if not exists checks_name_db
    on checks (name, database);

create table if not exists tags (
    name        text    primary key
);

create table if not exists checks_tags (
    num         integer not null,
    database    text    not null,
    tag         text    not null,
    primary key (num, database, tag),
    foreign key (num, database) references checks (num, database)
        on delete cascade,
    foreign key (tag)           references tags (name)
        on delete restrict
);

create table if not exists check_params (
    num             integer not null,
    database        text    not null,
    param           text    not null,
    value           text    not null,
    default_value   text    not null,
    primary key (num, database, param),
    foreign key (num, database) references checks (num, database)
        on delete cascade
);

create table if not exists plan_fields (
    database    text    not null,
    raw         text    not null,
    key         text    not null,
    enabled     integer not null default 999
                check (enabled in (0, 1, 999)),
    primary key (database, raw),
    foreign key (database) references databases (database)
        on delete cascade
);

create unique index if not exists plan_fields_key_db
    on plan_fields (key, database);

-- View used by `--show` and by any client that wants a compact list
-- of checks with their tags.
create view if not exists checks_with_tags as
select
    c.num,
    c.database,
    c.name,
    c.description,
    c.enabled,
    coalesce(
        (
            select group_concat(ct.tag, ', ')
            from checks_tags ct
            where ct.num = c.num and ct.database = c.database
        ),
        ''
    ) as tags
from checks c;

-- ============================================================
-- Config audit log
-- ============================================================
-- Persists across `--init`. The seed tables are dropped and
-- rebuilt; this one must survive.
--
-- `who` is set by the application via `_audit_session`; the
-- default is 'system' (e.g. `--init`, manual sqlite3 edits).

create table if not exists _audit_session (
    id   integer primary key check (id = 1),
    who  text    not null default 'system'
);

insert or ignore into _audit_session(id) values(1);

create table if not exists config_audit (
    id           integer primary key autoincrement,
    table_name   text    not null,
    column_name  text    not null,
    optype       text    not null check (optype in ('I', 'U', 'D')),
    ts           text    not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    old_value    text,
    new_value    text,
    who          text    not null default 'system'
);

create index if not exists config_audit_lookup
    on config_audit (table_name, column_name, ts desc);

create index if not exists config_audit_recent
    on config_audit (ts desc);

-- ============================================================
-- Audit triggers
-- ============================================================
-- For every audited column, three triggers: after insert,
-- after update, after delete. The update trigger is
-- double-filtered: `after update of <col>` fires only when
-- the column appears in the SET list, and
-- `when old.x is not new.x` skips no-op writes.
-- `is not` is NULL-safe in SQLite.

-- ============================================================
-- checks.enabled
-- ============================================================

create trigger if not exists trg_audit_checks_enabled_ins
after insert on checks
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('checks', 'enabled', 'I', null, cast(new.enabled as text),
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_checks_enabled_upd
after update of enabled on checks
for each row
when old.enabled is not new.enabled
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('checks', 'enabled', 'U',
         cast(old.enabled as text), cast(new.enabled as text),
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_checks_enabled_del
after delete on checks
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('checks', 'enabled', 'D', cast(old.enabled as text), null,
         (select who from _audit_session where id = 1));
end;

-- ============================================================
-- check_params.value
-- ============================================================

create trigger if not exists trg_audit_check_params_value_ins
after insert on check_params
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('check_params', 'value', 'I', null, new.value,
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_check_params_value_upd
after update of value on check_params
for each row
when old.value is not new.value
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('check_params', 'value', 'U', old.value, new.value,
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_check_params_value_del
after delete on check_params
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('check_params', 'value', 'D', old.value, null,
         (select who from _audit_session where id = 1));
end;

-- ============================================================
-- plan_fields.enabled
-- ============================================================

create trigger if not exists trg_audit_plan_fields_enabled_ins
after insert on plan_fields
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('plan_fields', 'enabled', 'I', null, cast(new.enabled as text),
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_plan_fields_enabled_upd
after update of enabled on plan_fields
for each row
when old.enabled is not new.enabled
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('plan_fields', 'enabled', 'U',
         cast(old.enabled as text), cast(new.enabled as text),
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_plan_fields_enabled_del
after delete on plan_fields
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('plan_fields', 'enabled', 'D',
         cast(old.enabled as text), null,
         (select who from _audit_session where id = 1));
end;

-- ============================================================
-- plan_fields.key
-- ============================================================

create trigger if not exists trg_audit_plan_fields_key_ins
after insert on plan_fields
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('plan_fields', 'key', 'I', null, new.key,
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_plan_fields_key_upd
after update of key on plan_fields
for each row
when old.key is not new.key
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('plan_fields', 'key', 'U', old.key, new.key,
         (select who from _audit_session where id = 1));
end;

create trigger if not exists trg_audit_plan_fields_key_del
after delete on plan_fields
for each row
begin
    insert into config_audit
        (table_name, column_name, optype, old_value, new_value, who)
    values
        ('plan_fields', 'key', 'D', old.key, null,
         (select who from _audit_session where id = 1));
end;

