
import os
import sys
from utils.helpers import load_config, get_env_path
from dotenv import load_dotenv

def check_health():
    print("🔍 Running health check...")
    
    # Check config files
    try:
        config = load_config()
        print("✅ Config file loaded successfully")
    except Exception as e:
        print(f"❌ Config file error: {e}")
        return False
    
    # Check environment variables
    try:
        env_path = get_env_path()
        load_dotenv(dotenv_path=env_path, override=True)
        
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            print("❌ No DISCORD_TOKEN found in .env")
            return False
        print("✅ Discord token found")
        
        # Check AI provider
        openai_key = os.getenv("OPENAI_API_KEY")
        groq_key = os.getenv("GROQ_API_KEY")
        
        if not openai_key and not groq_key:
            print("❌ No AI provider API key found")
            return False
        print("✅ AI provider API key found")
        
    except Exception as e:
        print(f"❌ Environment error: {e}")
        return False
    
    # Check database
    try:
        from utils.db import init_db
        init_db()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"❌ Database error: {e}")
        return False
    
    print("🎉 All health checks passed!")
    return True

if __name__ == "__main__":
    if not check_health():
        print("\n⚠️  Some issues were found. Please fix them before running the bot.")
        sys.exit(1)
    else:
        print("\n🚀 Bot is ready to run!")
