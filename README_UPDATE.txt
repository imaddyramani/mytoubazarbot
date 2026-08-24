MyTourBazar V168 - AI Assistant Freeform Tour Update

REPLACE ONLY:
- bot.py
- smart_assistant.py

DO NOT replace:
- start.py
- Dockerfile
- assets/
- data/
- .env

NEW BEHAVIOR:
1. Press AI Assistant and type naturally, no prefix:
   Make a 4 night / 5 day Goa package for Mr. Amit, 2 adults,
   3-star hotels, breakfast, private cab, North & South Goa sightseeing.

2. Short no-prefix format also works:
   Goa 4N 5D, Mr Amit, 2 adults, 3 star, breakfast, private cab,
   North and South Goa

3. Voice:
   Press AI Assistant -> send voice note -> speak naturally.
   The voice transcript goes through the same Tour creator.

4. Existing reference edit:
   Edit MTB12 and change Day 3 sightseeing.
   This routes to the saved-document editor.

5. If the request explicitly says "quotation" or "voucher", that selection is
   remembered. When you later choose Basic/Detailed PDF, the bot skips the
   redundant Quotation/Voucher question and uses the requested mode.

6. Supplier PDF/image/text behavior remains separate and unchanged.

MANUAL UPDATE:
Upload/replace bot.py and smart_assistant.py in the GitHub repository root,
commit, then redeploy the latest commit.
