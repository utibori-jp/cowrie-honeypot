# cowrie-honeypot

Cowrie honeypot on a DigitalOcean droplet, and the analysis that runs on its logs.

The honeypot takes SSH connections on port 22, records what attackers do, and
ships a day's log to Backblaze B2 once it has rotated. The analysis side reads
those logs back out of B2 and works out who the attackers are and where they
give up.

## Layout

```
vps/         runs on the droplet: cowrie config, rclone, the shipping cron
analytics/   runs on the k3s cluster: python package, image, notebooks
```

The split follows where things run. Files under `vps/` are placed by hand on a
single server. Files under `analytics/` are built into a container image and
deployed by Argo CD.

## What lives elsewhere

Deployment of the analysis lives in `homelab-services`: the Argo CD
Applications, the Helm charts, and the workflow definitions that say when each
job runs. This repository holds what goes inside the containers, not when they
are started.

## Data path

```
cowrie (droplet)
  var/log/cowrie/cowrie.json           rotated daily by twistd
    -> gzip, rclone copy                vps/scripts/export-logs.sh, hourly cron
      -> b2://cowrie-log/cowrie/honeypod1/year=/month=/day=/
        -> duckdb                       analytics/, daily workflow
          -> silver/events parquet      longhorn pvc on the cluster
```

Only the JSON logs leave the droplet. Malware samples under
`var/lib/cowrie/downloads/` stay local and are deleted after 30 days.
