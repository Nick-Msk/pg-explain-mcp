-- Configuration schema for pg-explain-mcp.
-- Loaded by `python -m pg_explain_mcp.config --init`.

create table if not exists checks (
    num         integer not null,
    database    text    not null,
    name        text    not null,   -- matches the Python class name
    description text    not null,
    enabled     integer not null default 1,
    primary key (num, database)
);

create unique index if not exists checks_name_db
    on checks (name, database);

create table if not exists check_params (
    num         integer not null,
    database    text    not null,
    param       text    not null,
    value       text    not null,
    primary key (num, database, param),
    foreign key (num, database) references checks (num, database)
        on delete cascade
);

