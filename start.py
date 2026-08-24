import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Maps (chat_id, original_message_id) -> replacement bot message_id
_PROGRESS_REPLACEMENTS = {}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"MyTourBazar bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[BOOT] Health server listening on port {port}", flush=True)
    server.serve_forever()


def _safe_send_kwargs(kwargs):
    allowed = {
        "parse_mode",
        "entities",
        "link_preview_options",
        "reply_markup",
        "message_thread_id",
        "business_connection_id",
        "protect_content",
    }
    return {
        key: value
        for key, value in kwargs.items()
        if key in allowed and value is not None
    }


def install_dynamic_progress_fix():
    from telegram import Bot, Message
    from telegram.error import BadRequest

    # ------------------------------------------------------------------
    # 1. Patch Message.edit_text
    #
    # If V167 tries to edit an incoming Air/Bus/Hotel upload message,
    # create ONE bot-owned status message and then edit that replacement
    # for all subsequent progress updates.
    # ------------------------------------------------------------------
    original_message_edit_text = Message.edit_text

    async def dynamic_message_edit_text(self, text, *args, **kwargs):
        chat_id = self.chat_id
        original_id = self.message_id
        key = (str(chat_id), int(original_id))

        replacement_id = _PROGRESS_REPLACEMENTS.get(key)

        # We already created a bot-owned replacement status message.
        if replacement_id is not None:
            try:
                return await self.get_bot().edit_message_text(
                    chat_id=chat_id,
                    message_id=replacement_id,
                    text=text,
                    **_safe_send_kwargs(kwargs),
                )
            except BadRequest as exc:
                msg = str(exc).lower()
                if "message is not modified" in msg:
                    return self
                # If even the replacement became non-editable, forget it and
                # create a new replacement below.
                if "can't be edited" not in msg and "can not be edited" not in msg:
                    raise
                _PROGRESS_REPLACEMENTS.pop(key, None)

        # An incoming user message can never be edited by the bot.
        sender_is_bot = bool(getattr(getattr(self, "from_user", None), "is_bot", False))

        if not sender_is_bot:
            new_msg = await self.get_bot().send_message(
                chat_id=chat_id,
                text=text,
                **_safe_send_kwargs(kwargs),
            )
            _PROGRESS_REPLACEMENTS[key] = new_msg.message_id
            print(
                f"[PROGRESS] Created bot progress message {new_msg.message_id} "
                f"for source message {original_id}",
                flush=True,
            )
            return new_msg

        # Normal bot-owned message: edit normally.
        try:
            return await original_message_edit_text(self, text, *args, **kwargs)
        except BadRequest as exc:
            msg = str(exc).lower()

            if "message is not modified" in msg:
                return self

            if "can't be edited" not in msg and "can not be edited" not in msg:
                raise

            new_msg = await self.get_bot().send_message(
                chat_id=chat_id,
                text=text,
                **_safe_send_kwargs(kwargs),
            )
            _PROGRESS_REPLACEMENTS[key] = new_msg.message_id
            print(
                f"[PROGRESS] Replaced non-editable bot message {original_id} "
                f"with {new_msg.message_id}",
                flush=True,
            )
            return new_msg

    Message.edit_text = dynamic_message_edit_text

    # ------------------------------------------------------------------
    # 2. Patch Bot.edit_message_text too.
    #
    # Some V167 paths call context.bot.edit_message_text directly instead
    # of Message.edit_text. Redirect those calls to the replacement ID.
    # ------------------------------------------------------------------
    original_bot_edit = Bot.edit_message_text

    async def dynamic_bot_edit(self, text, chat_id=None, message_id=None, *args, **kwargs):
        if chat_id is not None and message_id is not None:
            original_key = (str(chat_id), int(message_id))
            replacement_id = _PROGRESS_REPLACEMENTS.get(original_key)

            if replacement_id is not None:
                try:
                    return await original_bot_edit(
                        self,
                        text=text,
                        chat_id=chat_id,
                        message_id=replacement_id,
                        *args,
                        **kwargs,
                    )
                except BadRequest as exc:
                    msg = str(exc).lower()
                    if "message is not modified" in msg:
                        return None
                    if "can't be edited" not in msg and "can not be edited" not in msg:
                        raise
                    _PROGRESS_REPLACEMENTS.pop(original_key, None)

        try:
            return await original_bot_edit(
                self,
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                *args,
                **kwargs,
            )
        except BadRequest as exc:
            msg = str(exc).lower()

            if "message is not modified" in msg:
                return None

            if (
                chat_id is None
                or message_id is None
                or (
                    "can't be edited" not in msg
                    and "can not be edited" not in msg
                )
            ):
                raise

            new_msg = await self.send_message(
                chat_id=chat_id,
                text=text,
                **_safe_send_kwargs(kwargs),
            )
            _PROGRESS_REPLACEMENTS[(str(chat_id), int(message_id))] = new_msg.message_id

            print(
                f"[PROGRESS] Telegram rejected edit of {message_id}; "
                f"using new progress message {new_msg.message_id}",
                flush=True,
            )
            return new_msg

    Bot.edit_message_text = dynamic_bot_edit

    # ExtBot has its own implementation in python-telegram-bot.
    try:
        from telegram.ext import ExtBot
        ExtBot.edit_message_text = dynamic_bot_edit
    except Exception:
        pass

    print("[BOOT] Air/Bus/Hotel dynamic progress fix enabled.", flush=True)


def main():
    # Install patch before importing V167 bot.py.
    install_dynamic_progress_fix()

    health_thread = threading.Thread(
        target=run_health_server,
        name="back4app-health",
        daemon=True,
    )
    health_thread.start()

    import bot

    print("[BOOT] Starting MyTourBazar bot...", flush=True)
    bot.main()


if __name__ == "__main__":
    main()
