import os
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GPT_API_KEY = os.getenv("GPT_API_KEY")
GPT_ASSISTANT_ID = os.getenv("GPT_ASSISTANT_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

