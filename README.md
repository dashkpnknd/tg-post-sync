# Post Sync

Telegram control bot for copying a source channel into a destination channel.

All setup is performed in the bot UI:

- add a Telegram account by QR code or phone number;
- create as many source-to-destination tasks as needed;
- replace older destination posts with source posts, newest to newest;
- start or stop continuous copying of new source posts;
- see each task's status and progress.

## Install

1. Copy `.env.example` to `.env` and fill in the values.
2. Install `pip install -r requirements.txt`.
3. Run `python app.py`.

The account connected with Telethon must be able to read the source and publish/edit messages in the destination. Telegram does not allow changing a message date: history migration edits existing destination posts, preserving their original dates.

Never commit `.env` or `data.json`: they contain the bot token and Telegram account sessions.
