"""
CLI commands for Spotify account management.

Usage:
  antra spotify set-cookie <sp_dc> — use web player cookie (no restrictions)
  antra spotify set-token <token>  — use manual access token (emergency bypass)
  antra spotify login             — use PKCE OAuth (developer app needed)
  antra spotify status            — show current auth method
  antra spotify logout            — clear both auth methods
  antra spotify test-cookie       — verify cookie/token works
  antra spotify playlists         — list user's playlists
  antra spotify liked             — show liked songs count
"""
import argparse
import sys
import os
import json
from antra.core.config import load_config
from antra.core.spotify_auth import SpotifyAuthManager, SpotifyWebPlayerAuth
from antra.core.spotify_fetcher import SpotifyFetcher

def setup_spotify_cli(subparsers: argparse._SubParsersAction):
    sp_parser = subparsers.add_parser("spotify", help="Spotify account management")
    sp_sub = sp_parser.add_subparsers(dest="spotify_command")

    # set-cookie
    cookie_p = sp_sub.add_parser("set-cookie", help="Set Spotify web player cookie (sp_dc)")
    cookie_p.add_argument("cookie", help="The sp_dc cookie value from open.spotify.com")
    cookie_p.add_argument("--force", action="store_true", help="Save even if validation is blocked by 403")

    # set-token
    token_p = sp_sub.add_parser("set-token", help="Set manual Spotify access token (emergency bypass)")
    token_p.add_argument("cookie", help="The accessToken value from open.spotify.com Network tab")

    # test-cookie
    sp_sub.add_parser("test-cookie", help="Verify the current sp_dc cookie / token")

    # status
    status_p = sp_sub.add_parser("status", help="Show current Spotify authentication status")
    status_p.add_argument("--json", action="store_true", help="Output results in JSON format")

    # login (PKCE)
    login_p = sp_sub.add_parser("login", help="Open browser and log in to Spotify (developer app)")

    # logout
    logout_p = sp_sub.add_parser("logout", help="Log out and clear cached credentials")
    logout_p.add_argument("--json", action="store_true", help="Output results in JSON format")

    # playlists
    playlists_p = sp_sub.add_parser("playlists", help="List your Spotify playlists")
    playlists_p.add_argument("--json", action="store_true", help="Output results in JSON format")

    # liked
    liked_p = sp_sub.add_parser("liked", help="Show Liked Songs count")
    liked_p.add_argument("--json", action="store_true", help="Output results in JSON format")


def handle_spotify_cli(args, config):
    web_auth = None
    if config.spotify_sp_dc or config.spotify_access_token:
        web_auth = SpotifyWebPlayerAuth(
            sp_dc=config.spotify_sp_dc,
            manual_token=config.spotify_access_token
        )

    pkce_auth = SpotifyAuthManager(
        client_id=config.spotify_client_id,
        redirect_uri=config.spotify_redirect_uri,
        cache_path=config.spotify_cache_path,
    )

    fetcher = SpotifyFetcher(auth_manager=pkce_auth, web_player_auth=web_auth)
    cmd = args.spotify_command

    # ── set-cookie ──────────────────────────────────────────────────────────
    if cmd == "set-cookie":
        cookie_val = args.cookie.strip()
        force = getattr(args, "force", False)
        
        # 1. Validate
        test_auth = SpotifyWebPlayerAuth(sp_dc=cookie_val)
        is_valid = test_auth.is_valid(force_check=True)
        
        if is_valid or force:
            # 2. Save to .env
            from pathlib import Path
            env_path = Path(".env")
            content = ""
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
            
            if "SPOTIFY_SP_DC=" in content:
                import re
                new_content = re.sub(r'SPOTIFY_SP_DC=.*', f'SPOTIFY_SP_DC={cookie_val}', content)
            else:
                new_content = content.rstrip() + f"\nSPOTIFY_SP_DC={cookie_val}\n"
            
            env_path.write_text(new_content, encoding="utf-8")
            
            try:
                from dotenv import load_dotenv
                load_dotenv(override=True)
            except ImportError:
                pass

            if is_valid:
                print("✅ Web player cookie saved and verified")
            else:
                print("⚠️  Web player cookie saved (Validation Bypassed via --force)")
                print("Validation was blocked by a 403, but the app will try to use it during sync.")
            
            print("You can now sync your library.")
        else:
            print("\n❌ Cookie validation failed (403 Blocked).")
            print("Spotify is blocking the automated check on your IP.")
            print("\nOPTIONS:")
            print("1. Force save anyway:  antra spotify set-cookie <cookie> --force")
            print("2. Manual token:       antra spotify set-token <token>")
            sys.exit(1)

    # ── set-token ───────────────────────────────────────────────────────────
    elif cmd == "set-token":
        if not args.cookie:  # Using .cookie because of how subparser is shared
            print("❌ Access token required")
            sys.exit(1)
        
        token_val = args.cookie.strip()
        # 1. Validate
        test_auth = SpotifyWebPlayerAuth(manual_token=token_val)
        print("Verifying access token...")
        if test_auth.is_valid(force_check=True):
            # 2. Save to .env
            from pathlib import Path
            env_path = Path(".env")
            content = ""
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
            
            if "SPOTIFY_ACCESS_TOKEN=" in content:
                import re
                new_content = re.sub(r'SPOTIFY_ACCESS_TOKEN=.*', f'SPOTIFY_ACCESS_TOKEN={token_val}', content)
            else:
                new_content = content.rstrip() + f"\nSPOTIFY_ACCESS_TOKEN={token_val}\n"
            
            env_path.write_text(new_content, encoding="utf-8")
            
            try:
                from dotenv import load_dotenv
                load_dotenv(override=True)
            except ImportError:
                pass

            print("✅ Manual access token saved and verified")
            print("This token typically lasts 1 hour. Antra will use it until it expires.")
        else:
            print("❌ Token validation failed. Make sure you copied the 'accessToken' correctly.")
            sys.exit(1)

    # ── test-cookie ─────────────────────────────────────────────────────────
    elif cmd == "test-cookie":
        if not web_auth:
            print("❌ No sp_dc cookie set. Run: antra spotify set-cookie <sp_dc>")
            sys.exit(1)
        
        print("Verifying web player cookie...")
        if web_auth.is_valid(force_check=True):
            print("✅ Cookie is valid")
            try:
                playlists = fetcher.fetch_user_playlists()
                print(f"Successfully fetched {len(playlists)} playlists as proof:")
                for p in playlists[:3]:
                    print(f" - {p['name']}")
                if len(playlists) > 3:
                    print("   ...")
            except Exception as e:
                print(f"⚠️  Auth valid, but library fetch failed: {e}")
        else:
            print("❌ Cookie is invalid or expired. Please update it.")
            sys.exit(1)

    # ── status ─────────────────────────────────────────────────────────────
    elif cmd == "status":
        method = fetcher.get_auth_method()
        name = None
        if method == "pkce":
            name = pkce_auth.get_user_display_name()
        elif method == "web_player":
            name = "Web Player Cookie"

        if args.json:
            print(json.dumps({
                "authenticated": method != "none",
                "method": method,
                "name": name,
                "display_name": name or ("Authenticated" if method != "none" else None)
            }))
            return

        if method == "web_player":
            print("✅ Web Player Auth active (no API restrictions)")
            print("Cookie valid — no login required")
        elif method == "pkce":
            if name:
                print(f"✅ PKCE OAuth active (developer API) — Logged in as: {name}")
            else:
                print("✅ PKCE OAuth active (Profile hidden due to API restrictions)")
        else:
            print("❌ Not authenticated")
            print("Run: antra spotify set-cookie <sp_dc>")
            print("Or:  antra spotify login")

    # ── login (PKCE) ─────────────────────────────────────────────────────────
    elif cmd == "login":
        print("Opening Spotify for PKCE login (developer app flow)...")
        try:
            pkce_auth.authenticate()
            name = pkce_auth.get_user_display_name()
            if name:
                print(f"✅ Logged in as: {name}")
            else:
                print("✅ Authenticated!")
        except Exception as e:
            print(f"❌ Login failed: {e}", file=sys.stderr)
            sys.exit(1)

    # ── logout ─────────────────────────────────────────────────────────────
    elif cmd == "logout":
        pkce_auth.logout()
        # For cookie logout, we can't easily edit .env reliably, but we can tell user
        if args.json:
            print(json.dumps({"success": True, "message": "Credentials cleared"}))
        else:
            print("✅ PKCE session cleared.")
            if config.spotify_sp_dc:
                print("Note: To remove the web player cookie, delete SPOTIFY_SP_DC from your .env file.")

    # ── playlists ──────────────────────────────────────────────────────────
    elif cmd == "playlists":
        if fetcher.get_auth_method() == "none":
            if args.json:
                print(json.dumps({"error": "Not authenticated"}))
                sys.exit(1)
            print("❌ Not authenticated. Run: antra spotify set-cookie <sp_dc>")
            sys.exit(1)
        try:
            playlists = fetcher.fetch_user_playlists()
            if args.json:
                results = []
                for p in playlists:
                    results.append({
                        "name": p["name"],
                        "url": p["url"],
                        "tracks": p["track_count"],
                        "public": p.get("public", False)
                    })
                print(json.dumps(results))
                return

            print(f"\n{'NAME':<30} {'TRACKS':>6}  {'PUBLIC':<6}  URL")
            print("─" * 80)
            for p in playlists:
                pub = "Yes" if p.get("public") else "No"
                n = p["name"][:28]
                print(f"{n:<30} {p['track_count']:>6}  {pub:<6}  {p['url']}")
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            print(f"❌ Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── liked ──────────────────────────────────────────────────────────────
    elif cmd == "liked":
        if fetcher.get_auth_method() == "none":
            if args.json:
                print(json.dumps({"error": "Not authenticated"}))
                sys.exit(1)
            print("❌ Not authenticated")
            sys.exit(1)
        try:
            count = fetcher.fetch_liked_count()
            if args.json:
                print(json.dumps({"count": count}))
                return
            print(f"❤️  You have {count:,} Liked Songs.")
            ans = input("Download all? [y/N]: ").strip().lower()
            if ans == "y":
                tracks = fetcher.fetch_saved_tracks()
                return tracks
        except Exception as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        print("Usage: antra spotify [set-cookie|test-cookie|status|login|logout|playlists|liked]")
