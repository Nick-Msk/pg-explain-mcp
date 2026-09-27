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
    enabled     integer not null default 1,
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


