import os
import base64

def main():
    session_file = "telegram_forwarder_session.session"
    output_file = "session_base64.txt"
    
    if not os.path.exists(session_file):
        print(f"❌ Error: '{session_file}' not found in this folder.")
        print("Please run 'python main.py' locally first and complete the Telegram login once to generate it.")
        return
        
    try:
        with open(session_file, "rb") as f:
            file_data = f.read()
            b64_bytes = base64.b64encode(file_data)
            b64_string = b64_bytes.decode("utf-8")
            
        with open(output_file, "w") as out:
            out.write(b64_string)
            
        print("✅ Success!")
        print(f"The session file has been encoded to text and saved in '{output_file}'")
        print("\nHow to use:")
        print("1. Open the 'session_base64.txt' file in notepad/editor.")
        print("2. Copy the entire long text string.")
        print("3. In your Render Dashboard, go to your Web Service > Environment.")
        print("4. Add an Environment Variable:")
        print("   • Key: TELEGRAM_SESSION_BASE64")
        print("   • Value: (Paste the copied text string here)")
        print("5. Click Save.")
        
    except Exception as e:
        print(f"❌ Error occurred: {e}")

if __name__ == "__main__":
    main()
