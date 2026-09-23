-- Configuration schema for pg-explain-mcp.
-- Loaded by `python -m pg_explain_mcp.config --init`.

create table if not exists databases (
    database    text    primary key
);

create table if not exists checks (
    num         integer not null,
    database    text    not null,
    name        text    not null,   -- matches the Python class name
    description text    not null,
    enabled     integer not null default 1,
    primary key (num, database),
    foreign key (database) references databases (database)
        on delete cascade
);

create unique index if not exists checks_name_db
    on checks (name, database);

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
    database text    not null,
    raw      text    not null,   -- "Relation Name" — как в EXPLAIN JSON
    key      text    not null,   -- "relation" — как в plan_nodes
    enabled  integer not null default 1,
    primary key (database, raw),
    foreign key (database) references databases (database)
        on delete cascade
);

create unique index if not exists plan_fields_key_db
    on plan_fields (key, database);


