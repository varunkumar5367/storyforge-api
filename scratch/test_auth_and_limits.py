# scratch/test_auth_and_limits.py
import asyncio
import httpx
import sys
import uuid

BASE_URL = "http://localhost:8000"

async def test_auth_and_limits():
    print("=== STARTING BACKEND AUTH AND LIMITS E2E TEST ===")
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Verify health check
        try:
            resp = await client.get(f"{BASE_URL}/health")
            print(f"Health check: {resp.status_code} | {resp.json()}")
        except Exception as e:
            print(f"Backend not running or unreachable: {e}")
            sys.exit(1)

        # 2. Test Unauthenticated Access Blocked (401)
        resp = await client.get(f"{BASE_URL}/api/status/")
        print(f"Unauthenticated /api/status/: {resp.status_code} (Expected 401)")
        assert resp.status_code == 401, "Error: Unauthenticated access was not blocked!"

        # 3. Create a unique test user
        username = f"user_{uuid.uuid4().hex[:6]}"
        password = "TestPassword123"
        print(f"\nRegistering user: {username} ...")
        resp = await client.post(
            f"{BASE_URL}/api/auth/register",
            json={"username": username, "password": password}
        )
        print(f"Register status: {resp.status_code}")
        assert resp.status_code == 201, f"Failed to register: {resp.text}"
        user_id = resp.json()["id"]

        # 4. Log in to get token
        print("\nLogging in ...")
        resp = await client.post(
            f"{BASE_URL}/api/auth/login",
            json={"username": username, "password": password}
        )
        print(f"Login status: {resp.status_code}")
        assert resp.status_code == 200, f"Login failed: {resp.text}"
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 5. Verify Authenticated Access (200)
        resp = await client.get(f"{BASE_URL}/api/status/", headers=headers)
        print(f"Authenticated /api/status/: {resp.status_code} (Expected 200)")
        assert resp.status_code == 200, "Error: Authenticated access failed!"

        # 6. Test 1500-word upload cap for non-admin
        print("\nTesting word limit for non-admin user ...")
        long_story = "word " * 1501
        files = {
            "file": ("long_story.txt", long_story.encode("utf-8"), "text/plain")
        }
        resp = await client.post(
            f"{BASE_URL}/api/analyze/upload",
            files=files,
            data={"voice": "en-US-JennyNeural"},
            headers=headers
        )
        print(f"Upload 1501 words: {resp.status_code} (Expected 400)")
        assert resp.status_code == 400, "Error: 1500 word limit was not enforced!"
        print(f"Response: {resp.json().get('detail')}")

        # 7. Test successful upload for <= 1500 words
        print("\nTesting valid upload for non-admin user ...")
        short_story = "A short story with few words."
        files = {
            "file": ("short_story.txt", short_story.encode("utf-8"), "text/plain")
        }
        resp = await client.post(
            f"{BASE_URL}/api/analyze/upload",
            files=files,
            data={"voice": "en-US-JennyNeural"},
            headers=headers
        )
        print(f"Upload 6 words: {resp.status_code} (Expected 200)")
        assert resp.status_code == 200, f"Upload failed: {resp.text}"
        test_job_id = resp.json()["job_id"]
        print(f"Created job ID: {test_job_id}")

        # 8. Test admin login & seed check
        print("\nLogging in as seeded admin user 'varun5367' ...")
        admin_resp = await client.post(
            f"{BASE_URL}/api/auth/login",
            json={"username": "varun5367", "password": "Varun@5367"}
        )
        print(f"Admin Login: {admin_resp.status_code}")
        assert admin_resp.status_code == 200, f"Seeded admin login failed: {admin_resp.text}"
        admin_token = admin_resp.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        # 9. Test admin-only users list
        print("\nFetching user list as admin ...")
        users_resp = await client.get(f"{BASE_URL}/api/admin/users", headers=admin_headers)
        print(f"Admin /users list: {users_resp.status_code}")
        assert users_resp.status_code == 200, f"Admin failed to list users: {users_resp.text}"
        user_list = users_resp.json()
        print(f"Registered users: {[u['username'] for u in user_list]}")

        # 10. Test non-admin blocked from admin endpoints
        print("\nVerifying non-admin cannot access admin endpoints ...")
        users_fail_resp = await client.get(f"{BASE_URL}/api/admin/users", headers=headers)
        print(f"Non-admin accessing admin endpoint: {users_fail_resp.status_code} (Expected 403)")
        assert users_fail_resp.status_code == 403, "Error: Non-admin was not blocked from admin endpoint!"

        # 11. Test admin reads system logs
        print("\nVerifying admin uvicorn log reading ...")
        logs_resp = await client.get(f"{BASE_URL}/api/admin/logs", headers=admin_headers)
        print(f"Admin /logs: {logs_resp.status_code}")
        assert logs_resp.status_code == 200, f"Failed to get uvicorn logs: {logs_resp.text}"
        logs_data = logs_resp.json()
        print(f"Successfully read uvicorn logs (Total lines fetched: {len(logs_data.get('logs', []))})")

        # 12. Test admin deletes the test user
        print(f"\nDeleting test user '{username}' as admin ...")
        del_resp = await client.delete(f"{BASE_URL}/api/admin/users/{user_id}", headers=admin_headers)
        print(f"Delete user: {del_resp.status_code}")
        assert del_resp.status_code == 200, f"Failed to delete test user: {del_resp.text}"
        print("Delete Response:", del_resp.json())

        # Clean up job from DB
        await client.delete(f"{BASE_URL}/api/status/{test_job_id}", headers=admin_headers)

        print("\n=== ALL AUTH, ROLE AND LIMIT TESTS PASSED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(test_auth_and_limits())
