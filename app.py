import os
import re
import time
import pickle
import psycopg2
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from retro_sdk import Retro, FieldFilter
from datetime import datetime, timedelta, timezone

load_dotenv()

from urllib.parse import quote

def card_image_url(url):
    return f"https://wsrv.nl/?url={quote(url, safe='')}&w=1572&h=884&fit=contain&bg=transparent"

def avatar_image_url(url):
    return f"https://wsrv.nl/?url={quote(url, safe='')}&w=36&h=36&fit=cover"

def get_cursor():
    global conn
    try:
        conn.isolation_level
    except Exception:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        conn.autocommit = True
    return conn.cursor()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
conn.autocommit = True
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    slack_id TEXT PRIMARY KEY,
    retro_username TEXT,
    retro_blob BYTEA
)
""")
cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS retro_username TEXT")
cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS show_location BOOLEAN DEFAULT TRUE")

def save_retro_id(slack_id, user_id):
    c = get_cursor()
    c.execute(
        """
        INSERT INTO users (slack_id, retro_username) VALUES (%s, %s)
        ON CONFLICT (slack_id) DO UPDATE SET retro_username = EXCLUDED.retro_username
        """,
        (slack_id, user_id)
    )

def get_user_id(slack_id):
    c = get_cursor()
    c.execute("SELECT retro_username FROM users WHERE slack_id = %s", (slack_id,))
    row = c.fetchone()
    return row[0] if row else None

def get_show_location(slack_id):
    c = get_cursor()
    c.execute("SELECT show_location FROM users WHERE slack_id = %s", (slack_id,))
    row = c.fetchone()
    return row[0] if row is not None else True

def save_show_location(slack_id, value):
    c = get_cursor()
    c.execute(
        """
        INSERT INTO users (slack_id, show_location) VALUES (%s, %s)
        ON CONFLICT (slack_id) DO UPDATE SET show_location = EXCLUDED.show_location
        """,
        (slack_id, value)
    )

app = App(token=os.getenv("SLACK_BOT_TOKEN"))
retro = Retro(refresh_token=os.getenv("RETRO_REFRESH_TOKEN"))

selected_posts = {}  # slack_id -> set of post ids
selected_channels = {}  # slack_id -> {week -> channel_id}
home_cache = {}  # slack_id -> {week: [post, ...]}

@app.command("/link-retro-account")
def link_retro_account(ack, body, respond):
    ack()
    username = body.get("text", "").strip()
    if not username:
        respond("please provide your retro username: `/link-retro-account [your-username]`")
        return
    slack_id = body["user_id"]
    user_id = retro.get_user_id(username)
    if not user_id:
        respond(f"couldn't find a retro account with username *@{username}*. please check the spelling and try again.")
        return
    sent_request = retro.send_friend_request(user_id)
    if not sent_request:
        respond("failed to send friend request.")
        return
    save_retro_id(slack_id, user_id)
    respond(f"linked your slack account to retro user *@{username}*. make sure to accept the friend request from @hcslackforwarder!")

@app.command("/check-retro-link")
def check_retro_link(ack, body, respond):
    ack()
    slack_id = body["user_id"]
    user_id = get_user_id(slack_id)
    if not user_id:
        respond("you haven't linked your retro account yet!")
        return
    username = retro.get_user(user_id).get("username")
    if not username:
        respond("failed to retrieve your retro username.")
        return
    if not retro.get_friend_statuses(filter=FieldFilter("status", "==", "accepted")):
        respond(f"you haven't accepted the friend request from @hcslackforwarder yet! please accept it to complete the linking process.")
        return
    save_retro_id(slack_id, user_id)
    respond(f"your slack account is linked to retro user *@{username}*!")


def update_home_tab(event, client):
    print("loading...")
    user_id = event["user"]
    retro_user_id = get_user_id(user_id)

    if not retro.get_friend_statuses(filter=FieldFilter("status", "==", "accepted")):
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "you haven't accepted the friend request from @hcslackforwarder yet! please accept it to complete the linking process."}
            }
        ]

    elif not retro_user_id:
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "link your retro account to get started!"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "use `/link-retro-account [your-username]` in any channel to link your account."}
            }
        ]

    else:
        show_location = get_show_location(user_id)

        now = datetime.now()
        weeks = []
        for i in range(4):
            iso = (now - timedelta(weeks=i)).isocalendar()
            weeks.append(f"{iso[0]}_{iso[1]:02d}")

        if user_id not in home_cache:
            print("fetching from API...")
            fetched = {}
            for week in weeks:
                print(f"  fetching week {week}")
                posts = retro.get_week_media(retro_user_id, week)
                fetched[week] = sorted(posts, key=lambda p: p.get("createdAt") or 0)
            home_cache[user_id] = fetched
        else:
            print("using cache")

        retro_user = retro.get_user(retro_user_id) or {}

        toggle_option = {"text": {"type": "plain_text", "text": "Show locations"}, "value": "show_location"}
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"welcome, @{retro_user.get('username')}!"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "checkboxes",
                        "action_id": "toggle_show_location",
                        "options": [toggle_option],
                        "initial_options": [toggle_option] if show_location else []
                    },
                    {
                        "type": "button",
                        "action_id": "refresh_home",
                        "text": {"type": "plain_text", "text": "↻ refresh"}
                    }
                ]
            }
        ]

        for week in weeks:
            posts = home_cache[user_id].get(week, [])

            week_posts = []

            for i, post in enumerate(posts):
                comments = ""

                dt = datetime.fromtimestamp(post.get("createdAt"), tz=timezone(timedelta(seconds=post.get("timeZoneOffset") or 0)))

                if show_location and post.get("locationName"):
                    title = {
                        "title": {"type": "mrkdwn", "text": post.get("locationName"), "verbatim": False},
                        "subtitle": {"type": "mrkdwn", "text": dt.strftime("%a, %b %-d"), "verbatim": False},
                    }
                else:
                    title = {
                        "title": {"type": "mrkdwn", "text": dt.strftime("%A"), "verbatim": False},
                        "subtitle": {"type": "mrkdwn", "text": dt.strftime("%b %-d"), "verbatim": False},
                    }

                post_id = post.get("id")
                is_selected = post_id in selected_posts.get(user_id, set())
                card = {
                    "type": "card",
                    "block_id": f"carousel-card-{week}-{i}",
                    **title,
                    "hero_image": {
                        "type": "image",
                        "image_url": card_image_url(post.get("fullSizeURL")),
                        "alt_text": "photo"
                    },
                    "actions": [
                        {
                            "type": "button",
                            "action_id": "select_post",
                            "value": post_id,
                            "text": {"type": "plain_text", "text": "✓ selected" if is_selected else "select"},
                            **( {"style": "primary"} if is_selected else {} )
                        }
                    ]
                }
                if comments:
                    card["body"] = {"type": "mrkdwn", "text": comments, "verbatim": False}
                week_posts.append(card)

            if week_posts:
                blocks.append({"type": "header", "text": {"type": "plain_text", "text": f"Week {week.split('_')[1]}"}})
                blocks.append({"type": "carousel", "elements": week_posts[:10]})
                blocks.append({
                    "type": "actions",
                    "elements": [
                        {
                            "type": "conversations_select",
                            "action_id": f"pick_channel_{week}",
                            "placeholder": {"type": "plain_text", "text": "pick a channel"}
                        },
                        {
                            "type": "button",
                            "action_id": f"post_week_{week}",
                            "value": week,
                            "text": {"type": "plain_text", "text": "post selected"}
                        }
                    ]
                })

    client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    print("loaded")

@app.event("app_home_opened")
def on_home_opened(event, client):
    update_home_tab(event, client)

@app.action("toggle_show_location")
def handle_toggle_show_location(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    selected = body["actions"][0]["selected_options"]
    save_show_location(slack_id, len(selected) > 0)
    update_home_tab({"user": slack_id}, client)

@app.action("refresh_home")
def handle_refresh_home(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    home_cache.pop(slack_id, None)
    update_home_tab({"user": slack_id}, client)

@app.action("select_post")
def handle_select_post(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    post_id = body["actions"][0]["value"]
    if slack_id not in selected_posts:
        selected_posts[slack_id] = set()
    if post_id in selected_posts[slack_id]:
        selected_posts[slack_id].discard(post_id)
    else:
        selected_posts[slack_id].add(post_id)
    update_home_tab({"user": slack_id}, client)

@app.action(re.compile(r"pick_channel_(.+)"))
def handle_pick_channel(ack, body):
    ack()
    slack_id = body["user"]["id"]
    action = body["actions"][0]
    week = action["action_id"].removeprefix("pick_channel_")
    channel_id = action["selected_conversation"]
    selected_channels.setdefault(slack_id, {})[week] = channel_id

@app.action(re.compile(r"post_week_(.+)"))
def handle_post_week(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week = body["actions"][0]["value"]
    channel_id = selected_channels.get(slack_id, {}).get(week)
    if not channel_id:
        client.chat_postEphemeral(channel=slack_id, user=slack_id, text="pick a channel first!")
        return
    posts = home_cache.get(slack_id, {}).get(week, [])
    user_selected = selected_posts.get(slack_id, set())
    to_post = [p for p in posts if p.get("id") in user_selected]
    if not to_post:
        client.chat_postEphemeral(channel=slack_id, user=slack_id, text="no posts selected for this week!")
        return
    for post in to_post:
        client.chat_postMessage(
            channel=channel_id,
            blocks=[
                {
                    "type": "image",
                    "image_url": post.get("fullSizeURL"),
                    "alt_text": "retro photo"
                }
            ]
        )
        selected_posts[slack_id].discard(post.get("id"))

@app.action("unlink_retro_account")
def unlink_retro_account(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    get_cursor().execute("UPDATE users SET retro_username = NULL WHERE slack_id = %s", (slack_id,))
    update_home_tab({"user": slack_id}, client)

if __name__ == "__main__":
    SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN")).start()
