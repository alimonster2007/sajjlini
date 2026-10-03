import os
import sqlite3
import requests

# --- الإعدادات ومسارات المشروع ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'database.db')  # تعديل اسم قاعدة البيانات حسب مشروعك
MATERIALS_DIR = os.path.join(BASE_DIR, 'static', 'materials')

# جلب بيانات البوت من متغيرات البيئة أو وضعها مباشرة للทดربة
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def upload_file_to_telegram(file_path):
    """رفع الملف إلى التليغرام واسترجاع telegram_file_id"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ أخطاء: لم يتم العثور على TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID في ملف .env")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, 'rb') as file:
            response = requests.post(
                url,
                data={'chat_id': TELEGRAM_CHAT_ID, 'caption': f"Backup: {os.path.basename(file_path)}"},
                files={'document': file}
            )
        result = response.json()
        if result.get('ok'):
            return result['result']['document']['file_id']
        else:
            print(f"❌ فشل الرفع إلى تلغرام: {result.get('description')}")
            return None
    except Exception as e:
        print(f"❌ خطأ أثناء رفع الملف {file_path}: {e}")
        return None

def sync_local_materials():
    """فحص قاعدة البيانات والمجلد المحلي ومزامنة الملفات غير المرفوعة"""
    if not os.path.exists(DB_PATH):
        print(f"❌ لم يتم العثور على قاعدة البيانات في: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # جلب جميع المواد المسجلة
    cursor.execute("SELECT id, file_path, telegram_file_id, drive_url FROM materials")
    materials = cursor.fetchall()

    synced_count = 0
    missing_local_files = 0

    print(f"🔍 تم العثور على {len(materials)} سجل في جدول materials...\n")

    for item in materials:
        mat_id, file_name, telegram_id, drive_url = item

        if not file_name:
            continue

        # تحديد المسار المحلي للملف
        local_file_path = os.path.join(MATERIALS_DIR, file_name) if not os.path.isabs(file_name) else file_name

        # المزامنة إذا كان الملف متوفراً محلياً ولا يملك telegram_file_id
        if os.path.exists(local_file_path):
            if not telegram_id:
                print(f"⬆️ جاري رفع الملف المحلي: {file_name}...")
                new_telegram_id = upload_file_to_telegram(local_file_path)

                if new_telegram_id:
                    cursor.execute("""
                        UPDATE materials 
                        SET telegram_file_id = ? 
                        WHERE id = ?
                    """, (new_telegram_id, mat_id))
                    conn.commit()
                    synced_count += 1
                    print(f"✅ تمت المزامنة بنجاح للمادة ID: {mat_id} (Telegram ID: {new_telegram_id})\n")
            else:
                print(f"ℹ️ الملف {file_name} مرفوع سابقاً للسحابة.")
        else:
            if not telegram_id and not drive_url:
                print(f"⚠️ الملف غير موجود محلياً ولا يوجد له رابط سحابي: {file_name}")
                missing_local_files += 1

    conn.close()
    print("=" * 50)
    print(f"✨ اكتملت العملية:")
    print(f" - ملفات تمت مزامنتها بنجاح: {synced_count}")
    print(f" - ملفات مفقودة محلياً وسحابياً: {missing_local_files}")

if __name__ == '__main__':
    sync_local_materials()