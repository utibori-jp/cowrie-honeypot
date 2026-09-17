# VPS side

Everything here is placed by hand on the droplet. There is no automated
deployment: one server, set up once.

## Files

```
cowrie/cowrie.cfg.example      the parts of cowrie.cfg that differ from stock
rclone/rclone.conf.example     goes to /root/.config/rclone/rclone.conf, mode 600
scripts/export-logs.sh         hourly cron, ships rotated logs to B2
```

Both `.example` files carry placeholders. Fill them in on the server and do not
copy the filled version back here.

## Setup notes

Cowrie listens on 22 through authbind, running as a non-root user. Port 22
matters because that is where the scanning traffic is; on 2222 the sample size
collapses.

```
sudo apt install -y authbind
sudo touch /etc/authbind/byport/22
sudo chown cowrie:cowrie /etc/authbind/byport/22
sudo chmod 770 /etc/authbind/byport/22
AUTHBIND_ENABLED=yes cowrie start
```

Admin SSH moved to 2222 first. After editing sshd_config, check `ss -tlnp`
rather than trusting `sshd -T`: Ubuntu's ssh.service uses KillMode=process, so
the old listener survives a restart and keeps holding port 22. Kill the stale
PID by hand, with a second session already open on 2222.

## Shipping

The cron runs hourly but only ever ships files cowrie has already rotated, so
in practice each day's log goes out on the first run after midnight. The
active file is never touched.

The B2 key on this side is append-only. That breaks `rclone copy`'s default
existence check, which needs read access, hence `--no-check-dest`. Rotated
file names are unique per day, so skipping the check costs nothing.
