

import os
import time
import sqlite3
import pickle
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from RetroSDK import Retro


load_dotenv()

DB_PATH = "retro_users.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    slack_id TEXT PRIMARY KEY,
    retro_blob BLOB
)
""")
conn.commit()

def save_user(slack_id, retro_obj):
    blob = pickle.dumps(retro_obj)
    cur.execute("REPLACE INTO users (slack_id, retro_blob) VALUES (?, ?)", (slack_id, blob))
    conn.commit()

def load_user(slack_id):
    cur.execute("SELECT retro_blob FROM users WHERE slack_id = ?", (slack_id,))
    row = cur.fetchone()
    if row:
        return pickle.loads(row[0])
    return None

app = App(token=os.getenv("SLACK_BOT_TOKEN"))

# Global rate limit: timestamp of last code request
last_code_request_time = 0


# In-memory cache for active users (optional, for performance)
users = {}

# Catch-all action handler for debugging
@app.action({"type": "block_actions"})
def catch_all_actions(ack, body, logger):
    ack()
    print("[DEBUG] Received action:")
    print(body)


# App Home opened event: show Home tab with login button
@app.event("app_home_opened")
def update_home_tab(event, client, logger):
    user_id = event["user"]
    client.views_publish(
        user_id=user_id,
        view={
            "type": "home",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "welcome!"}
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "open_login_modal",
                            "text": {"type": "plain_text", "text": "log in to retro"}
                        }
                    ]
                }
            ]
        }
    )

@app.action("open_login_modal")
def handle_open_login_modal(ack, body, client):
    ack()
    print("pressed")
    user_id = body["user"]["id"]
    if user_id not in users:
        retro = load_user(user_id)
        if retro is None:
            retro = Retro()
        users[user_id] = retro
        save_user(user_id, retro)
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "phone_modal",
            "title": {"type": "plain_text", "text": "log in to retro!"},
            "submit": {"type": "plain_text", "text": "send code"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "phone_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "phone_input",
                        "placeholder": {"type": "plain_text", "text": "+1234567890"}
                    },
                    "label": {"type": "plain_text", "text": "please enter your phone number. it must be in international format (for example, if you have an american number, begin it with +1)."}
                }
            ]
        }
    )

@app.view("phone_modal")
def handle_phone_submission(ack, body, client, view):
    ack()

    global last_code_request_time
    user_id = body["user"]["id"]

    if user_id not in users:
        retro = load_user(user_id)
        if retro is not None:
            users[user_id] = retro
        else:
            ack()
            client.chat_postMessage(
                channel=user_id,
                text="please start over! there's no login in progress"
            )
            return

    phone = view["state"]["values"]["phone_block"]["phone_input"]["value"].replace(" ", "").replace("-", "")
    now = time.time()
    if now - last_code_request_time < 60:
        ack()
        client.chat_postMessage(
            channel=user_id,
            text="please wait - you can only request one code per minute. (this limit is across all users)"
        )
        return
    try:
        users[user_id].send_code(phone)
        print("sent code!")
        last_code_request_time = now
        save_user(user_id, users[user_id])
    except Exception as e:
        ack()
        client.chat_postMessage(
            channel=user_id,
            text=f"Failed to send code: {e}"
        )
        print("failed to send (?):", e)
        return
    # Update the modal in-place to prompt for code
    print("updating modal")
    ack(
        response_action="update",
        view={
            "type": "modal",
            "callback_id": "code_modal",
            "title": {"type": "plain_text", "text": "enter verification code"},
            "submit": {"type": "plain_text", "text": "verify"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "code_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "code_input",
                        "placeholder": {"type": "plain_text", "text": "123456"}
                    },
                    "label": {"type": "plain_text", "text": "please enter the code you received"}
                }
            ]
        }
    )

@app.view("code_modal")
def handle_code_submission(ack, body, client, view):
    print("entered code!")
    try:
        user_id = body["user"]["id"]
        code = view["state"]["values"]["code_block"]["code_input"]["value"]
        ack()  # Always ack first
        if user_id not in users:
            retro = load_user(user_id)
            if retro is not None:
                users[user_id] = retro
            else:
                client.chat_postMessage(
                    channel=user_id,
                    text="please start over! there's no login in progress"
                )
                return
        try:
            users[user_id].verify_code(code)
            save_user(user_id, users[user_id])
            client.chat_postMessage(
                channel=user_id,
                text="you're logged in!"
            )
        except Exception as e:
            client.chat_postMessage(
                channel=user_id,
                text=f"verification failed: {e}"
            )
    except Exception as e:
        # Failsafe: always ack to avoid Slack modal error
        try:
            ack()
        except Exception:
            pass
        print(f"[ERROR] Exception in code_modal handler: {e}")

if __name__ == "__main__":
    SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN")).start()