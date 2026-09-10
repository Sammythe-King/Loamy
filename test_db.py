"""
Quick smoke test: proves database.py can talk to Supabase end-to-end.
Run it with:  python test_db.py

It creates a throwaway user + one goal, reads them back, prints the result,
then deletes the user (which cascade-deletes the goal) so nothing is left behind.
If you see "ALL GOOD" at the end, your database is working.
"""

import database

TEST_USER_ID = "test_user_smoke"
TEST_EMAIL = "smoke_test@loamy.local"


def main():
    print("[1/5] Cleaning up any leftover test user...")
    # delete_user isn't a public helper, so just try to remove via the client.
    try:
        database.supabase.table("users").delete().eq("user_id", TEST_USER_ID).execute()
    except Exception as e:
        print("   (cleanup skipped:", e, ")")

    print("[2/5] Creating a test user...")
    created = database.create_user(
        user_id=TEST_USER_ID,
        email=TEST_EMAIL,
        password_hash="not_a_real_hash",
        full_name="Smoke Test",
        business_type="retail",
        country="NG",
    )
    print("   ->", created["status"], "| error:", created["error"])
    if created["status"] != "success":
        print("FAILED at create_user. Stop here and read the error above.")
        return

    print("[3/5] Adding a goal for that user...")
    goal = database.add_goal(
        user_id=TEST_USER_ID, goal_id="goal_smoke_1",
        item="Test Goal", amount=5000, deadline="Monthly", category="goal",
    )
    print("   ->", goal["status"], "| error:", goal["error"])

    print("[4/5] Reading the user + goals back...")
    fetched_user = database.get_user_by_email(TEST_EMAIL)
    fetched_goals = database.get_goals(TEST_USER_ID)
    print("   user found:", bool(fetched_user["data"]))
    print("   goals found:", len(fetched_goals["data"] or []))

    print("[5/5] Cleaning up (deleting test user)...")
    database.supabase.table("users").delete().eq("user_id", TEST_USER_ID).execute()

    ok = (
        created["status"] == "success"
        and goal["status"] == "success"
        and bool(fetched_user["data"])
        and len(fetched_goals["data"] or []) >= 1
    )
    print("\n==============================")
    print("ALL GOOD - your database works!" if ok else "SOMETHING FAILED - see messages above.")
    print("==============================")


if __name__ == "__main__":
    main()
