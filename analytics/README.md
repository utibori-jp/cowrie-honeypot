# Analytics

Reads the Cowrie logs out of B2 and turns them into something you can ask
questions of. Runs on the k3s cluster, both as scheduled jobs and as the
environment behind Jupyter.

## Layers

```
bronze   the gzipped JSON lines in B2, untouched
silver   the same events as parquet on a longhorn pvc, partitioned by day
gold     features, clusters, survival curves
```

Bronze stays in B2 and is never copied down whole. Silver exists so that
exploring does not mean re-reading B2 on every query.

Beside those sits a Postgres that Grafana reads, filled by `publish` from
silver. It holds three tables at session grain: `sessions`, `login_attempts`
and `commands`. Nothing originates there. Losing it costs a re-run of
`publish` over the days on the volume, which is why it runs on one replica
with no backup.

Grafana gets SQL rather than a pile of pre-computed aggregates because the
dashboard is for looking around, and aggregates only answer the questions
somebody thought of while writing them. Prometheus would not do: source IPs,
usernames, passwords and command strings each run to thousands of distinct
values, and a time series per label combination is not what any of that is.

The normalize job that produces `silver/events` makes no modelling decisions.
It writes whatever columns Cowrie emitted that day, so the schema is Cowrie's,
documented at https://docs.cowrie.org/en/latest/OUTPUT.html. Two things are
not Cowrie's: `source_file`, the B2 object a row came from, and `year`,
`month`, `day`, which come from the directory names rather than the files.

From a notebook it is a view called `silver_events`, created by `init-views`
and stored in the workbench database:

```sql
%%sql
SELECT eventid, count(*) FROM silver_events GROUP BY 1 ORDER BY 2 DESC
```

From code that has its own connection, `db.silver_events_source()` is the same
thing as a SQL expression. Either way, do not hand write `read_parquet`: both
pin `union_by_name`, without which the read breaks once two days disagree on
columns, and pin the partition columns to VARCHAR, which DuckDB otherwise
guesses per column and gets inconsistent.

One process at a time can hold the workbench database open for writing, so
jobs connect in memory instead. Nothing a job does needs the file.

Field sets differ sharply by event type. A day of scanning is almost entirely
session and login events, while the interesting ones (`file_download`,
`client.fingerprint`, `malformed_packet`) show up a handful of times and carry
fields nothing else does. That is why the job infers its schema from every
line rather than a sample.

## Running it

```
honeypot-analytics init-views                     (re)create the notebook views
honeypot-analytics init-db                        (re)create the postgres tables
honeypot-analytics normalize                      yesterday
honeypot-analytics normalize --date 2026-09-15    one day
honeypot-analytics normalize --date ""              also yesterday, for argo
honeypot-analytics normalize --from 2026-09-13 --to 2026-09-15
honeypot-analytics publish                        yesterday, silver to postgres
honeypot-analytics publish --from 2026-09-13 --to 2026-09-20
honeypot-analytics report                         yesterday, postgres to discord
```

Each run replaces one day, so re-running is safe and backfilling is a loop over
dates. `publish` takes the same date arguments as `normalize` and replaces the
day in all three tables inside one transaction.

A day with no silver partition stops `publish` rather than emptying that day in
Postgres, since the delete would land and the insert would bring nothing.
`--allow-missing` turns that into a skip, leaving whatever is already stored.

`report` reads Postgres rather than silver, so the numbers it posts and the
numbers on the dashboard cannot disagree.

Losing the Postgres volume costs `init-db` and then `publish` over the days on
the data volume. Neither happens on its own, so a rebuilt database stays empty
until someone runs them.

## Environment

```
AWS_ACCESS_KEY_ID        b2 key id, read only
AWS_SECRET_ACCESS_KEY    b2 application key
HONEYPOT_B2_PREFIX       s3://cowrie-log/cowrie/honeypod1
HONEYPOT_B2_ENDPOINT     s3.us-west-004.backblazeb2.com
HONEYPOT_B2_REGION       us-west-004
HONEYPOT_DATA_DIR        where silver and gold are written
PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD    where publish writes
HONEYPOT_PG_DSN          a whole connection string, instead of the above
DISCORD_WEBHOOK_URL      where report posts
```

On the cluster the two credentials come from the `b2-credentials` sealed
secret and the rest from the chart's values.

The Postgres connection is spelled out as libpq variables so that only
`PGPASSWORD` has to come from a secret and the rest stays readable in the
chart. `HONEYPOT_PG_DSN` exists for pointing at a throwaway database while
developing, and wins when it is set.

## Development

```
pip install -e '.[dev]'
pytest
```

The tests build small gzipped JSON files locally and run the real normalize
path over them, so they cover the DuckDB SQL without touching B2. The same
fixtures feed `publish`, whose transform is a SELECT against silver and needs
no database to check.

The handful of tests that do write to Postgres skip unless `HONEYPOT_PG_DSN`
points somewhere throwaway:

```
docker run -d --rm --name pg -e POSTGRES_PASSWORD=devonly \
  -e POSTGRES_DB=honeypot -p 55432:5432 postgres:18-alpine
HONEYPOT_PG_DSN=postgresql://postgres:devonly@localhost:55432/honeypot pytest
```
