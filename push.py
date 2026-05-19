"""
Optional Gmail push notifications via Google Cloud Pub/Sub.

When PUBSUB_PROJECT_ID and PUBSUB_SUBSCRIPTION_ID are set in .env,
the agent switches from polling to real-time push — emails are processed
the instant they arrive instead of waiting up to POLL_INTERVAL seconds.

Setup (one-time, ~5 minutes):
  1. In Google Cloud Console → Pub/Sub → Topics → Create topic
     Name it: gmail-notifications

  2. Grant publish permission to Gmail's service account:
     Principal:  serviceAccount:gmail-api-push@system.gserviceaccount.com
     Role:       Pub/Sub Publisher

  3. Create a pull subscription on the topic:
     Subscription name: gmail-agent-sub
     Delivery: Pull

  4. Add to .env:
     PUBSUB_PROJECT_ID=your-gcp-project-id
     PUBSUB_SUBSCRIPTION_ID=gmail-agent-sub

  5. Gmail watch registration (handled automatically on startup — expires
     every 7 days, renewed automatically each run).
"""

import logging
import threading

log = logging.getLogger(__name__)

_WATCH_RENEWAL_KEY = "gmail_watch_expires_at"
_WATCH_RENEW_BUFFER = 86400  # renew 1 day before expiry


def setup_watch(gmail_service, topic_name: str, db) -> None:
    """Register Gmail push notifications to the given Pub/Sub topic.
    Re-registers automatically if expiring within 24 hours.
    topic_name format: 'projects/PROJECT_ID/topics/TOPIC_NAME'
    """
    import time

    expires_at = float(db.get_state(_WATCH_RENEWAL_KEY, "0"))
    if time.time() < expires_at - _WATCH_RENEW_BUFFER:
        log.info("Gmail watch still valid (expires in %.0fh)", (expires_at - time.time()) / 3600)
        return

    result = gmail_service.users().watch(
        userId="me",
        body={"topicName": topic_name, "labelIds": ["INBOX"]},
    ).execute()

    new_expiry = int(result["expiration"]) / 1000  # ms → seconds
    db.set_state(_WATCH_RENEWAL_KEY, str(new_expiry))
    log.info("Gmail watch registered — expires %s", _fmt_ts(new_expiry))


def run_push_loop(
    project_id: str,
    subscription_id: str,
    on_notification,          # callable() — triggered on each Pub/Sub message
) -> None:
    """Block forever, calling on_notification() each time Gmail pushes a message.
    Runs the Pub/Sub subscriber in the current thread.
    Falls back gracefully if google-cloud-pubsub is not installed.
    """
    try:
        from google.cloud import pubsub_v1
    except ImportError:
        log.error(
            "google-cloud-pubsub not installed — cannot use push mode. "
            "Run: pip install google-cloud-pubsub"
        )
        return

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(project_id, subscription_id)

    def _callback(message):
        message.ack()
        log.info("Push notification received — processing new emails")
        try:
            on_notification()
        except Exception as exc:
            log.error("Error handling push notification: %s", exc, exc_info=True)

    log.info("Listening on Pub/Sub subscription: %s", subscription_path)
    future = subscriber.subscribe(subscription_path, callback=_callback)

    try:
        future.result()  # blocks until cancelled or error
    except Exception as exc:
        log.error("Pub/Sub stream ended: %s", exc)
        future.cancel()


def _fmt_ts(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
