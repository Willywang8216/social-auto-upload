import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from conf import BASE_DIR
from utils.profile_pipeline import ensure_profile_tables
from utils.publish_jobs import ensure_publish_job_tables

# 数据库文件路径（如果不存在会自动创建）
db_file = Path(BASE_DIR / 'db' / 'database.db')
db_file.parent.mkdir(parents=True, exist_ok=True)

# 如果数据库已存在，则删除旧的表（可选）
# if os.path.exists(db_file):
#     os.remove(db_file)

# 连接到SQLite数据库（如果文件不存在则会自动创建）
conn = sqlite3.connect(str(db_file))
cursor = conn.cursor()

# 创建账号记录表
cursor.execute('''
CREATE TABLE IF NOT EXISTS user_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type INTEGER NOT NULL DEFAULT 0,
    filePath TEXT NOT NULL DEFAULT '',  -- 存储文件路径
    userName TEXT NOT NULL,
    status INTEGER DEFAULT 0,
    platform_key TEXT,
    auth_mode TEXT DEFAULT 'qr_cookie',
    metadata_json TEXT DEFAULT '{}'
)
''')

# 创建文件记录表
cursor.execute('''CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 唯一标识每条记录
    filename TEXT NOT NULL,               -- 文件名
    filesize REAL,                     -- 文件大小（单位：MB）
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP, -- 上传时间，默认当前时间
    file_path TEXT                        -- 文件路径
)
''')


# 提交更改
conn.commit()
ensure_profile_tables(Path(db_file))
ensure_publish_job_tables(Path(db_file))
print("✅ 表创建成功")
# 关闭连接
conn.close()
