#!/usr/bin/env python3
import sqlite3
import time
import os
from datetime import datetime

DB_FILE = "guild_tracker.db"
BACKUP_DIR = "backups"

def create_backup():
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    backup_file = f"{BACKUP_DIR}/backup_{timestamp}.db"

    try:
        conn = sqlite3.connect(DB_FILE)
        backup_conn = sqlite3.connect(backup_file)
        conn.backup(backup_conn)
        print(f"Backup created: {backup_file}")
        return True
    except Exception as e:
        print(f"Backup failed: {str(e)}")
        return False
    finally:
        conn.close()
        backup_conn.close()

if __name__ == "__main__":
    create_backup()