# Backups + external dead-man ping (one-time setup, ~10 minutes)

Two gaps this closes:
1. **`btc.db` is the only copy** of the subscriber emails and the entire runs
   history (score chart, streaks, forward-test). One disk loss destroys both.
2. **Every alert — including the watchdog — originates on this box.** If the
   whole box dies, the failure mode is pure silence.

Both need credentials only the owner can create. Everything else is staged.

## 1. Litestream → S3 (continuous SQLite backup)

In the AWS console (or CloudShell):

```bash
# a) bucket (pick a globally-unique name; region must match deploy/litestream.yml)
aws s3 mb s3://YOUR_BUCKET_NAME --region us-east-1

# b) minimal IAM user + key
aws iam create-user --user-name litestream-btc
aws iam put-user-policy --user-name litestream-btc --policy-name litestream-s3 \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],
    "Resource":["arn:aws:s3:::YOUR_BUCKET_NAME","arn:aws:s3:::YOUR_BUCKET_NAME/*"]}]}'
aws iam create-access-key --user-name litestream-btc   # note the key id + secret
```

On the box (`ssh -i ~/.ssh/lightsail.pem ubuntu@44.212.248.190`):

```bash
wget -q https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-0.3.13-linux-amd64.deb -O /tmp/litestream.deb
sudo dpkg -i /tmp/litestream.deb

# config: copy the repo template and fill the bucket name
sudo cp ~/btc-accumulation-notifier/deploy/litestream.yml /etc/litestream.yml
sudo sed -i 's/YOUR_BUCKET_NAME/<the bucket>/' /etc/litestream.yml

# credentials (NOT in the yml)
printf 'AWS_ACCESS_KEY_ID=...\nAWS_SECRET_ACCESS_KEY=...\n' | sudo tee /etc/default/litestream
sudo chmod 600 /etc/default/litestream

sudo systemctl enable --now litestream
systemctl status litestream --no-pager        # should be active, no errors
```

Verify with a test restore (do this once — an unverified backup is a hope, not a backup):

```bash
litestream restore -o /tmp/btc-restore.db s3://<the bucket>/btc-accumulation-notifier/btc.db
sqlite3 /tmp/btc-restore.db "SELECT COUNT(*) FROM runs; SELECT COUNT(*) FROM subscribers;"
rm /tmp/btc-restore.db
```

## 2. External dead-man ping (healthchecks.io)

Create a free check at https://healthchecks.io → name it `btc-watchdog`, set
**Period = 1 hour, Grace = 30 min** (the watchdog cron runs hourly), and copy
the ping URL. Then on the box, `crontab -e` and extend the watchdog line:

```
0 * * * * cd /home/ubuntu/btc-accumulation-notifier && /home/ubuntu/btc-accumulation-notifier/.venv/bin/python -m app.watchdog >> /home/ubuntu/btc-accumulation-notifier/logs/watchdog.log 2>&1 && curl -fsS -m 10 https://hc-ping.com/YOUR-UUID > /dev/null
```

`&&` is deliberate: the ping only fires when the watchdog ran AND exited 0, so a
crash-looping watchdog also surfaces as a missed ping. healthchecks.io then
emails you from OUTSIDE the box — covering whole-box death, wiped crontab, and
watchdog crashes. (It still does not cover silently-dead Resend credentials;
the watchdog's own stale-pipeline email covers in-box pipeline failures.)
