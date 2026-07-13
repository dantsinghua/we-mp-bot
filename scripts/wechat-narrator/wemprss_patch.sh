#!/bin/bash
# Idempotent WN-PATCH applier for we-mp-rss container.
# Adds a 300s hang guard around the playwright content fetch (upstream has no
# timeout: a hung fetch permanently jams the content queue). Timeout raises ->
# upstream's built-in web->api fallback + fix_fail_count(3-strike skip) engage.
# Run after any container RECREATE (docker restart keeps the patch).
set -e
docker exec -i we-mp-rss python3 - <<'EOF'
p = "/app/driver/wxarticle.py"
s = open(p).read()
old = "result = loop.run_until_complete(fetcher.get_article_content(url))"
new = "result = loop.run_until_complete(asyncio.wait_for(fetcher.get_article_content(url), timeout=300))  # WN-PATCH: hang guard"
if "WN-PATCH" in s:
    print("wemprss_patch: already applied")
elif old in s:
    open(p, "w").write(s.replace(old, new, 1))
    print("wemprss_patch: applied")
else:
    raise SystemExit("wemprss_patch: ANCHOR NOT FOUND - upstream changed, re-port the patch")
EOF
docker exec we-mp-rss python3 -m py_compile /app/driver/wxarticle.py
echo "wemprss_patch: compile OK (restart container to load: docker restart we-mp-rss)"
