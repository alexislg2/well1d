### Permet de charger a posteriori les points que le raspberry n'a pas pu transmettre

import sqlite3
import os

FAILED_DATABASE = 'failed_uploads.db'
WELL_DATABASE = 'well.db'

def import_failed_uploads():
    # Connect to the databases
    failed_conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.realpath(__file__)), FAILED_DATABASE))
    well_conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.realpath(__file__)), WELL_DATABASE))

    failed_cursor = failed_conn.cursor()
    well_cursor = well_conn.cursor()

    # Get all records from the failed_uploads.db
    failed_cursor.execute("SELECT timestamp, height_mm FROM failed_uploads")
    failed_records = failed_cursor.fetchall()

    # Prepare to insert data into well.db without duplicates
    for record in failed_records:
        timestamp, height_mm = record

        # Check if the record already exists in well.db
        well_cursor.execute("SELECT COUNT(*) FROM water_height WHERE timestamp = ?", (timestamp,))
        exists = well_cursor.fetchone()[0]

        if exists == 0:
            # If the record doesn't exist, insert it
            well_cursor.execute("INSERT INTO water_height (timestamp, height_mm) VALUES (?, ?)", (timestamp, height_mm))
            print(f"Inserted record with timestamp: {timestamp}")

    # Commit the changes and close the connections
    well_conn.commit()
    failed_conn.close()
    well_conn.close()

if __name__ == "__main__":
    import_failed_uploads()