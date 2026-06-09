import os
import re
import time
import pickle
import threading
import requests
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
cur.execute("""
CREATE TABLE IF NOT EXISTS post_channels (
    post_id TEXT,
    channel_id TEXT,
    PRIMARY KEY (post_id, channel_id)
)
""")

def record_post_channel(post_id, channel_id):
    c = get_cursor()
    c.execute(
        "INSERT INTO post_channels (post_id, channel_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (post_id, channel_id)
    )

def get_post_channels(post_ids):
    if not post_ids:
        return {}
    c = get_cursor()
    c.execute("SELECT post_id, channel_id FROM post_channels WHERE post_id = ANY(%s)", (list(post_ids),))
    result = {}
    for post_id, channel_id in c.fetchall():
        result.setdefault(post_id, []).append(channel_id)
    return result

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

def build_card(post, week, index, show_location, block_id_prefix="carousel-card", selected=False, show_actions=True, posted_channels=None, removed_locs=None):
    is_video = bool(post.get("videoURL") or post.get("originalVideoURL"))
    prefix = "[Video] " if is_video else ""
    post_id = post.get("id")
    dt = datetime.fromtimestamp(post.get("createdAt"), tz=timezone(timedelta(seconds=post.get("timeZoneOffset") or 0)))
    channel_suffix = " · " + " ".join(f"<#{c}>" for c in posted_channels) if posted_channels else ""
    loc_removed = removed_locs and post_id in removed_locs

    if show_location and post.get("locationName") and not loc_removed:
        title = {
            "title": {"type": "mrkdwn", "text": prefix + post.get("locationName"), "verbatim": False},
            "subtitle": {"type": "mrkdwn", "text": dt.strftime("%a, %b %-d") + channel_suffix, "verbatim": False},
        }
    else:
        title = {
            "title": {"type": "mrkdwn", "text": prefix + dt.strftime("%A"), "verbatim": False},
            "subtitle": {"type": "mrkdwn", "text": dt.strftime("%b %-d") + channel_suffix, "verbatim": False},
        }

    card = {
        "type": "card",
        "block_id": f"{block_id_prefix}-{week}-{index}",
        **title,
        "hero_image": {
            "type": "image",
            "image_url": card_image_url(post.get("fullSizeURL")),
            "alt_text": "retro photo"
        },
    }
    if show_actions:
        card["actions"] = [
            {
                "type": "button",
                "action_id": "select_post",
                "value": post_id,
                "text": {"type": "plain_text", "text": "Selected" if selected else "Select"},
                **( {"style": "primary"} if selected else {} )
            },
            *( [{
                "type": "button",
                "action_id": "home_remove_location",
                "value": post_id,
                "text": {"type": "plain_text", "text": "Include location" if loc_removed else "Remove location"}
            }] if post.get("locationName") else [] )
        ]
    return card

def build_dm_card(post, week, index, excluded, removed_locs):
    is_video = bool(post.get("videoURL") or post.get("originalVideoURL"))
    prefix = "[Video] " if is_video else ""
    post_id = post.get("id")
    value = f"{week}|{post_id}"
    dt = datetime.fromtimestamp(post.get("createdAt"), tz=timezone(timedelta(seconds=post.get("timeZoneOffset") or 0)))
    show_loc = post.get("locationName") and post_id not in removed_locs
    if show_loc:
        title = {
            "title": {"type": "mrkdwn", "text": prefix + post.get("locationName"), "verbatim": False},
            "subtitle": {"type": "mrkdwn", "text": dt.strftime("%a, %b %-d"), "verbatim": False},
        }
    else:
        title = {
            "title": {"type": "mrkdwn", "text": prefix + dt.strftime("%A"), "verbatim": False},
            "subtitle": {"type": "mrkdwn", "text": dt.strftime("%b %-d"), "verbatim": False},
        }
    is_excluded = post_id in excluded
    return {
        "type": "card",
        "block_id": f"dm-card-{week}-{index}",
        **title,
        "hero_image": {"type": "image", "image_url": card_image_url(post.get("fullSizeURL")), "alt_text": "retro photo"},
        "actions": [
            {
                "type": "button",
                "action_id": "dm_toggle_exclude",
                "value": value,
                "text": {"type": "plain_text", "text": "Include" if is_excluded else "Exclude"},
                **( {} if is_excluded else {"style": "danger"} )
            },
            *( [{
                "type": "button",
                "action_id": "dm_remove_location",
                "value": value,
                "text": {"type": "plain_text", "text": "Include location" if post_id in removed_locs else "Remove location"}
            }] if post.get("locationName") else [] )
        ]
    }

def build_dm_blocks(slack_id, week):
    key = (slack_id, week)
    posts = dm_pending.get(slack_id, {}).get(week, {}).get("posts", [])
    excluded = dm_excluded_posts.get(key, set())
    removed_locs = dm_removed_locations.get(key, set())
    cards = [build_dm_card(p, week, i, excluded, removed_locs) for i, p in enumerate(posts)]
    week_num = int(week.split("_")[1])
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{len(posts)} new post{'s' if len(posts) != 1 else ''} in Week {week_num}!* Click *Exclude* on any picture to exclude it from posting."}}]
    for i in range(0, len(cards), 10):
        blocks.append({"type": "carousel", "elements": cards[i:i + 10]})
        blocks.append({"type": "divider"})
    selected_ch = dm_selected_channels.get(key)
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "conversations_select",
                "action_id": "dm_pick_channel",
                "placeholder": {"type": "plain_text", "text": "Pick a channel"},
                **({"initial_conversation": selected_ch} if selected_ch else {})
            },
            {
                "type": "button",
                "action_id": "dm_post",
                "style": "primary",
                "value": week,
                "text": {"type": "plain_text", "text": "Post to channel"}
            }
        ]
    })
    return blocks

def send_or_update_dm(slack_id, week, new_posts, client):
    if not new_posts:
        return
    week_pending = dm_pending.setdefault(slack_id, {})
    existing = week_pending.get(week)
    if existing:
        existing_ids = {x.get("id") for x in existing["posts"]}
        existing["posts"] = existing["posts"] + [p for p in new_posts if p.get("id") not in existing_ids]
        client.chat_update(channel=existing["channel"], ts=existing["ts"], blocks=build_dm_blocks(slack_id, week), text="New retro posts!")
    else:
        dm_ch = client.conversations_open(users=slack_id)["channel"]["id"]
        week_pending[week] = {"channel": dm_ch, "ts": None, "posts": new_posts}
        result = client.chat_postMessage(channel=dm_ch, text="New retro posts!", blocks=build_dm_blocks(slack_id, week))
        week_pending[week]["ts"] = result["ts"]

app = App(token=os.getenv("SLACK_BOT_TOKEN"))
retro = Retro(refresh_token=os.getenv("RETRO_REFRESH_TOKEN"))

selected_posts = {}  # slack_id -> set of post ids
selected_channels = {}  # slack_id -> {week -> channel_id}
home_cache = {}  # slack_id -> {week: [post, ...]}
dm_excluded_posts = {}  # (slack_id, week) -> set of post_ids excluded from DM post
dm_removed_locations = {}  # (slack_id, week) -> set of post_ids with location stripped
dm_pending = {}  # slack_id -> {week -> {channel, ts, posts}}
dm_selected_channels = {}  # (slack_id, week) -> channel_id for DM post
home_removed_locations = {}  # slack_id -> set of post_ids with location stripped on home tab
home_selected_week = {}  # slack_id -> week string (currently shown week)

@app.command("/link-retro-account")
def link_retro_account(ack, body, respond):
    ack()
    username = body.get("text", "").strip()
    if not username:
        respond("Please provide your retro username: `/link-retro-account [your-username]`")
        return
    slack_id = body["user_id"]
    user_id = retro.get_user_id(username)
    if not user_id:
        respond(f"Couldn't find a retro account with username *@{username}*. Please check the spelling and try again.")
        return
    sent_request = retro.send_friend_request(user_id)
    if not sent_request:
        respond(f"Couldn't send a friend request to *@{username}* — your friend requests are likely locked! Unlock friend requests in their retro settings and try again.")
        return
    save_retro_id(slack_id, user_id)
    respond(f"Linked your Slack account to retro user *@{username}*. Make sure to accept the friend request from @hcslackforwarder!")

@app.command("/check-retro-link")
def check_retro_link(ack, body, respond):
    ack()
    slack_id = body["user_id"]
    user_id = get_user_id(slack_id)
    if not user_id:
        respond("You haven't linked your retro account yet!")
        return
    username = retro.get_user(user_id).get("username")
    if not username:
        respond("Failed to retrieve your retro username.")
        return
    if not retro.get_friend_statuses(filter=FieldFilter("status", "==", "accepted")):
        respond(f"You haven't accepted the friend request from @hcslackforwarder yet! Please accept it to complete the linking process.")
        return
    save_retro_id(slack_id, user_id)
    respond(f"Your Slack account is linked to retro user *@{username}*!")


def update_home_tab(event, client):
    print("loading...")
    user_id = event["user"]
    retro_user_id = get_user_id(user_id)

    if not retro.get_friend_statuses(filter=FieldFilter("status", "==", "accepted")):
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "You haven't accepted the friend request from @hcslackforwarder yet! Please accept it to complete the linking process."}
            }
        ]

    elif not retro_user_id:
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Link your retro account to get started!"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Use `/link-retro-account [your-username]` in any channel to link your account."}
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

        active_week = home_selected_week.get(user_id, weeks[0])
        if active_week not in weeks:
            active_week = weeks[0]
            home_selected_week[user_id] = active_week

        toggle_option = {"text": {"type": "plain_text", "text": "Show locations"}, "value": "show_location"}
        week_options = [{"text": {"type": "plain_text", "text": f"Week {int(w.split('_')[1])}"}, "value": w} for w in weeks]
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f":hyper-dino-wave: Welcome @{retro_user.get('username')}!", "emoji": True}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":warning: Because this page is very slow to update, use the select buttons and then click `Load selections` to view what you've selected."}
            },
            { "type": "divider" },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "load_selections",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Load selections"}
                    },
                    {
                        "type": "button",
                        "action_id": "refresh_home",
                        "text": {"type": "plain_text", "text": "Refresh posts"}
                    },
                    {
                        "type": "checkboxes",
                        "action_id": "toggle_show_location",
                        "options": [toggle_option],
                        "initial_options": [toggle_option] if show_location else []
                    },
                    {
                        "type": "static_select",
                        "action_id": "pick_week",
                        "options": week_options,
                        "initial_option": {"text": {"type": "plain_text", "text": f"Week {int(active_week.split('_')[1])}"}, "value": active_week}
                    }
                ]
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "load_selections",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Load selections"}
                    }
                ]
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"select_all_{active_week}",
                        "value": active_week,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Select all"}
                    },
                    {
                        "type": "button",
                        "action_id": f"deselect_all_{active_week}",
                        "value": active_week,
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Deselect all"}
                    },
                    {
                        "type": "button",
                        "action_id": f"select_last_5min_{active_week}",
                        "value": active_week,
                        "text": {"type": "plain_text", "text": "Posted within 5 min"}
                    },
                    {
                        "type": "button",
                        "action_id": f"select_recent_unposted_{active_week}",
                        "value": active_week,
                        "text": {"type": "plain_text", "text": "Recent unposted to Slack"}
                    },
                    {
                        "type": "button",
                        "action_id": f"select_by_time_{active_week}",
                        "value": active_week,
                        "text": {"type": "plain_text", "text": "Select by posted time"}
                    }
                ]
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "conversations_select",
                        "action_id": f"pick_channel_{active_week}",
                        "placeholder": {"type": "plain_text", "text": "Pick a channel"}
                    },
                    {
                        "type": "button",
                        "action_id": f"post_week_{active_week}",
                        "value": active_week,
                        "text": {"type": "plain_text", "text": "Post selected"}
                    }
                ]
            }
        ]

        active_posts = home_cache[user_id].get(active_week, [])
        channels_by_post = get_post_channels([p.get("id") for p in active_posts])
        removed_locs = home_removed_locations.get(user_id, set())

        week = active_week
        posts = active_posts

        week_posts = []

        for i, post in enumerate(posts):
            is_selected = post.get("id") in selected_posts.get(user_id, set())
            week_posts.append(build_card(post, week, i, show_location, selected=is_selected, posted_channels=channels_by_post.get(post.get("id")), removed_locs=removed_locs))

        if week_posts:
            chunks = [week_posts[i:i + 10] for i in range(0, len(week_posts), 10)]
            for chunk in chunks:
                blocks.append({"type": "carousel", "elements": chunk})
                blocks.append({"type": "divider"})

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
    try:
        refresh_and_notify(slack_id, client)
    except Exception as e:
        print(f"refresh failed: {e}")
        home_cache.pop(slack_id, None)
    update_home_tab({"user": slack_id}, client)

@app.action("pick_week")
def handle_pick_week(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    home_selected_week[slack_id] = body["actions"][0]["selected_option"]["value"]
    update_home_tab({"user": slack_id}, client)

@app.action("home_remove_location")
def handle_home_remove_location(ack, body):
    ack()
    slack_id = body["user"]["id"]
    post_id = body["actions"][0]["value"]
    locs = home_removed_locations.setdefault(slack_id, set())
    if post_id in locs:
        locs.discard(post_id)
    else:
        locs.add(post_id)

@app.action("select_post")
def handle_select_post(ack, body):
    ack()
    slack_id = body["user"]["id"]
    post_id = body["actions"][0]["value"]
    if slack_id not in selected_posts:
        selected_posts[slack_id] = set()
    if post_id in selected_posts[slack_id]:
        selected_posts[slack_id].discard(post_id)
    else:
        selected_posts[slack_id].add(post_id)

@app.action("load_selections")
def handle_load_selections(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    update_home_tab({"user": slack_id}, client)

@app.action("dm_remove_location")
def handle_dm_remove_location(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week, post_id = body["actions"][0]["value"].split("|", 1)
    key = (slack_id, week)
    locs = dm_removed_locations.setdefault(key, set())
    if post_id in locs:
        locs.discard(post_id)
    else:
        locs.add(post_id)
    info = dm_pending.get(slack_id, {}).get(week)
    if info:
        client.chat_update(channel=info["channel"], ts=info["ts"], blocks=build_dm_blocks(slack_id, week), text="New retro posts!")

@app.action("dm_toggle_exclude")
def handle_dm_toggle_exclude(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week, post_id = body["actions"][0]["value"].split("|", 1)
    key = (slack_id, week)
    excluded = dm_excluded_posts.setdefault(key, set())
    if post_id in excluded:
        excluded.discard(post_id)
    else:
        excluded.add(post_id)
    info = dm_pending.get(slack_id, {}).get(week)
    if info:
        client.chat_update(channel=info["channel"], ts=info["ts"], blocks=build_dm_blocks(slack_id, week), text="New retro posts!")

@app.action("dm_pick_channel")
def handle_dm_pick_channel(ack, body):
    ack()
    slack_id = body["user"]["id"]
    msg_ts = body.get("container", {}).get("message_ts")
    for week, info in dm_pending.get(slack_id, {}).items():
        if info.get("ts") == msg_ts:
            dm_selected_channels[(slack_id, week)] = body["actions"][0]["selected_conversation"]
            break

@app.action("dm_post")
def handle_dm_post(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week = body["actions"][0]["value"]
    key = (slack_id, week)
    channel_id = dm_selected_channels.get(key)
    info = dm_pending.get(slack_id, {}).get(week)
    if not channel_id or not info:
        return
    excluded = dm_excluded_posts.get(key, set())
    posts_to_send = [p for p in info["posts"] if p.get("id") not in excluded]
    removed_locs = dm_removed_locations.get(key, set())
    show_location = get_show_location(slack_id)
    if not posts_to_send:
        return
    channels_by_post = get_post_channels([p.get("id") for p in posts_to_send])
    cards = [build_card(p, week, i, show_location and p.get("id") not in removed_locs, block_id_prefix="dm-post-card", show_actions=False, posted_channels=channels_by_post.get(p.get("id"))) for i, p in enumerate(posts_to_send)]
    carousel_msg = client.chat_postMessage(channel=channel_id, text="New retro posts", blocks=[{"type": "carousel", "elements": cards[:10]}])
    thread_ts = carousel_msg["ts"]
    for post in posts_to_send:
        post_id = post.get("id")
        is_video = bool(post.get("videoURL") or post.get("originalVideoURL"))
        if is_video and post.get("originalVideoURL"):
            try:
                client.conversations_join(channel=channel_id)
            except Exception:
                pass
            video_data = requests.get(post["originalVideoURL"]).content
            client.files_upload_v2(channel=channel_id, thread_ts=thread_ts, content=video_data, filename=f"{post_id}.mov", title=post.get("locationName") or "Video")
        elif post.get("fullSizeURL"):
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=" ", blocks=[{"type": "image", "image_url": post["fullSizeURL"], "alt_text": "retro photo"}])
        record_post_channel(post_id, channel_id)
    dm_pending.get(slack_id, {}).pop(week, None)
    dm_excluded_posts.pop(key, None)
    dm_removed_locations.pop(key, None)
    dm_selected_channels.pop(key, None)
    client.chat_update(channel=info["channel"], ts=info["ts"], text=f"Posted {len(posts_to_send)} photo{'s' if len(posts_to_send) != 1 else ''} to <#{channel_id}>.", blocks=[])

@app.action(re.compile(r"^select_all_(.+)"))
def handle_select_all(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week = body["actions"][0]["value"]
    posts = home_cache.get(slack_id, {}).get(week, [])
    selected_posts.setdefault(slack_id, set()).update(p.get("id") for p in posts)
    update_home_tab({"user": slack_id}, client)

@app.action(re.compile(r"deselect_all_(.+)"))
def handle_deselect_all(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week = body["actions"][0]["value"]
    posts = home_cache.get(slack_id, {}).get(week, [])
    for p in posts:
        selected_posts.get(slack_id, set()).discard(p.get("id"))
    update_home_tab({"user": slack_id}, client)

@app.action(re.compile(r"select_last_5min_(.+)"))
def handle_select_last_5min(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week = body["actions"][0]["value"]
    cutoff = time.time() - 5 * 60
    posts = home_cache.get(slack_id, {}).get(week, [])
    selected_posts.setdefault(slack_id, set()).update(
        p.get("id") for p in posts if (p.get("uploadedAt") or 0) >= cutoff
    )
    update_home_tab({"user": slack_id}, client)

@app.action(re.compile(r"select_recent_unposted_(.+)"))
def handle_select_recent_unposted(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week = body["actions"][0]["value"]
    cutoff = time.time() - 5 * 60
    posts = home_cache.get(slack_id, {}).get(week, [])
    recent = [p for p in posts if (p.get("uploadedAt") or 0) >= cutoff]
    channels = get_post_channels([p.get("id") for p in recent])
    selected_posts.setdefault(slack_id, set()).update(
        p.get("id") for p in recent if not channels.get(p.get("id"))
    )
    update_home_tab({"user": slack_id}, client)

@app.action(re.compile(r"select_by_time_(.+)"))
def handle_select_by_time(ack, body, client):
    ack()
    week = body["actions"][0]["value"]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "select_by_time_modal",
            "private_metadata": week,
            "title": {"type": "plain_text", "text": "Select by time"},
            "submit": {"type": "plain_text", "text": "Select"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "amount_block",
                    "label": {"type": "plain_text", "text": "Created in the last..."},
                    "element": {
                        "type": "number_input",
                        "action_id": "amount",
                        "is_decimal_allowed": False,
                        "min_value": "1"
                    }
                },
                {
                    "type": "input",
                    "block_id": "unit_block",
                    "label": {"type": "plain_text", "text": "Minutes / Hours / Days"},
                    "element": {
                        "type": "static_select",
                        "action_id": "unit",
                        "options": [
                            {"text": {"type": "plain_text", "text": "minutes"}, "value": "60"},
                            {"text": {"type": "plain_text", "text": "hours"}, "value": "3600"},
                            {"text": {"type": "plain_text", "text": "days"}, "value": "86400"}
                        ]
                    }
                }
            ]
        }
    )

@app.view("select_by_time_modal")
def handle_select_by_time_submit(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    week = body["view"]["private_metadata"]
    values = body["view"]["state"]["values"]
    amount = int(values["amount_block"]["amount"]["value"])
    unit_seconds = int(values["unit_block"]["unit"]["selected_option"]["value"])
    cutoff = datetime.now(tz=timezone.utc).timestamp() - (amount * unit_seconds)
    posts = home_cache.get(slack_id, {}).get(week, [])
    selected_posts.setdefault(slack_id, set()).update(
        p.get("id") for p in posts if (p.get("uploadedAt") or 0) >= cutoff
    )
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
        client.chat_postEphemeral(channel=slack_id, user=slack_id, text="Pick a channel first!")
        return
    posts = home_cache.get(slack_id, {}).get(week, [])
    user_selected = selected_posts.get(slack_id, set())
    to_post = [p for p in posts if p.get("id") in user_selected]
    if not to_post:
        client.chat_postEphemeral(channel=slack_id, user=slack_id, text="No posts selected for this week!")
        return

    show_location = get_show_location(slack_id)
    removed_locs = home_removed_locations.get(slack_id, set())
    cards = [
        build_card(post, week, i, show_location, block_id_prefix="post-card", show_actions=False, removed_locs=removed_locs)
        for i, post in enumerate(to_post)
    ]

    resp = client.chat_postMessage(
        channel=channel_id,
        text="retro photos",
        blocks=[{"type": "carousel", "elements": cards[:10]}]
    )
    thread_ts = resp["ts"]
    for post in to_post:
        if post.get("originalVideoURL"):
            client.conversations_join(channel=channel_id)
            video_data = requests.get(post["originalVideoURL"]).content
            client.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                content=video_data,
                filename="retro-video.mov",
                title="video"
            )
        else:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="retro photo",
                blocks=[{"type": "image", "image_url": post.get("fullSizeURL"), "alt_text": "retro photo"}]
            )
        record_post_channel(post.get("id"), channel_id)
        selected_posts[slack_id].discard(post.get("id"))
    update_home_tab({"user": slack_id}, client)

@app.action("unlink_retro_account")
def unlink_retro_account(ack, body, client):
    ack()
    slack_id = body["user"]["id"]
    get_cursor().execute("UPDATE users SET retro_username = NULL WHERE slack_id = %s", (slack_id,))
    update_home_tab({"user": slack_id}, client)

def refresh_and_notify(slack_id, client):
    retro_user_id = get_user_id(slack_id)
    if not retro_user_id:
        return
    now = datetime.now()
    weeks = [f"{(now - timedelta(weeks=i)).isocalendar()[0]}_{(now - timedelta(weeks=i)).isocalendar()[1]:02d}" for i in range(4)]
    old_ids = {p.get("id") for week_posts in home_cache.get(slack_id, {}).values() for p in week_posts}
    fetched = {}
    for week in weeks:
        posts = retro.get_week_media(retro_user_id, week)
        fetched[week] = sorted(posts, key=lambda p: p.get("createdAt") or 0)
    home_cache[slack_id] = fetched
    if old_ids:
        for week, week_posts in fetched.items():
            new_posts = [p for p in week_posts if p.get("id") not in old_ids]
            if new_posts:
                send_or_update_dm(slack_id, week, new_posts, client)

def refresh_cache_loop():
    while True:
        time.sleep(30)
        for slack_id in list(home_cache.keys()):
            try:
                refresh_and_notify(slack_id, app.client)
                print(f"  refreshed cache for {slack_id}")
            except Exception as e:
                print(f"  failed to refresh cache for {slack_id}: {e}")

if __name__ == "__main__":
    threading.Thread(target=refresh_cache_loop, daemon=True).start()
    SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN")).start()
