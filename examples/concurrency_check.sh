#!/usr/bin/env bash
# 并发复验：配方草稿写（PUT/DELETE）与发布（publish）竞速时的原子胜负顺序。
#
# 针对本地运行的 API（默认 http://localhost:8000，可用 BASE_URL 覆盖）：
#
#   docker compose up --build        # 先启动服务
#   bash examples/concurrency_check.sh
#
# 每轮同时发出两个竞争请求，断言数据库层按版本状态决定的胜负顺序：
#
#   竞速 A  PUT vs publish   —— 发布先提交：PUT 必须 409 RECIPE_VERSION_IMMUTABLE，
#                              已发布行及其内容不变；PUT 先提交：发布冻结更新后的草稿。
#   竞速 B  DELETE vs publish —— 发布先提交：DELETE 必须 409，已发布行不被删除；
#                              DELETE 先提交：发布必须 404 RECIPE_VERSION_NOT_FOUND，
#                              且不留任何快照。
#   竞速 C  PUT vs DELETE    —— DELETE 先提交：PUT 必须 404；PUT 先提交：DELETE 204。
#
# 任何其他结果都视为回归，脚本以非零状态退出。
set -uo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
ROUNDS="${ROUNDS:-10}"
failures=0
declare -A seen=()

say()  { printf '%s\n' "$*"; }
fail() { say "  FAIL: $*"; failures=$((failures + 1)); }

# http METHOD PATH [BODY] -> 输出响应体，最后一行为 HTTP 状态码
http() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body" -w $'\n%{http_code}'
  else
    curl -sS -X "$method" "$BASE$path" -w $'\n%{http_code}'
  fi
}

status_of() { tail -n1 <<<"$1"; }
body_of()   { sed '$d' <<<"$1"; }

err_code() { python3 -c 'import sys,json; print(json.load(sys.stdin).get("detail",{}).get("code",""))' <<<"$1" 2>/dev/null; }
field()    { python3 -c "import sys,json; print(json.load(sys.stdin)$1)" <<<"$2" 2>/dev/null; }

mk_recipe() { # code -> 创建配方 + 草稿 v1（10 g sugar）
  local code="$1"
  http POST /recipes "{\"code\":\"$code\",\"name\":\"$code\"}" >/dev/null
  http POST "/recipes/$code/versions" \
    '{"version":1,"items":[{"kind":"ingredient","code":"sugar","quantity":10,"unit":"g"}]}' >/dev/null
}

check_health() {
  curl -fsS "$BASE/health" >/dev/null 2>&1 || {
    say "API 不可达：$BASE/health —— 请先执行 docker compose up --build"
    exit 2
  }
}

race_put_vs_publish() { # 竞速 A
  local round="$1" code="race-a-$round-$$"
  mk_recipe "$code"

  local put_out pub_out
  put_out=$(mktemp); pub_out=$(mktemp)
  http PUT "/recipes/$code/versions/1" \
    '{"items":[{"kind":"ingredient","code":"sugar","quantity":99,"unit":"g"}]}' >"$put_out" &
  http POST "/recipes/$code/versions/1/publish" >"$pub_out" &
  wait

  local put_code put_body pub_code
  put_code=$(status_of "$(cat "$put_out")"); put_body=$(body_of "$(cat "$put_out")")
  pub_code=$(status_of "$(cat "$pub_out")")
  rm -f "$put_out" "$pub_out"

  local final final_status final_qty
  final=$(body_of "$(http GET "/recipes/$code/versions/1")")
  final_status=$(field "['status']" "$final")
  final_qty=$(field "['items'][0]['quantity']" "$final")

  if [ "$put_code" = "200" ]; then
    # PUT 先提交：发布必须成功并冻结更新后的草稿（99 g）。
    seen[A_put_first]=1
    [ "$pub_code" = "200" ] || fail "A#$round PUT 200 但 publish=$pub_code（应 200）"
    [ "$final_status" = "published" ] || fail "A#$round 最终状态=$final_status（应 published）"
    [ "$final_qty" = "99.0" ] || fail "A#$round 发布内容=$final_qty g（应冻结 PUT 后的 99 g）"
  elif [ "$put_code" = "409" ] && [ "$(err_code "$put_body")" = "RECIPE_VERSION_IMMUTABLE" ]; then
    # 发布先提交：旧 PUT 必须 409，已发布内容（10 g）不变。
    seen[A_publish_first]=1
    [ "$pub_code" = "200" ] || fail "A#$round PUT 409 但 publish=$pub_code（应 200）"
    [ "$final_status" = "published" ] || fail "A#$round 最终状态=$final_status（应 published）"
    [ "$final_qty" = "10.0" ] || fail "A#$round 已发布内容被改写为 $final_qty g（应保持 10 g）"
  else
    fail "A#$round PUT 返回 $put_code $(err_code "$put_body")（应 200 或 409 RECIPE_VERSION_IMMUTABLE）"
  fi
}

race_delete_vs_publish() { # 竞速 B
  local round="$1" code="race-b-$round-$$"
  mk_recipe "$code"

  local del_out pub_out
  del_out=$(mktemp); pub_out=$(mktemp)
  http DELETE "/recipes/$code/versions/1" >"$del_out" &
  http POST "/recipes/$code/versions/1/publish" >"$pub_out" &
  wait

  local del_code del_body pub_code pub_body
  del_code=$(status_of "$(cat "$del_out")"); del_body=$(body_of "$(cat "$del_out")")
  pub_code=$(status_of "$(cat "$pub_out")"); pub_body=$(body_of "$(cat "$pub_out")")
  rm -f "$del_out" "$pub_out"

  local get_code
  get_code=$(status_of "$(http GET "/recipes/$code/versions/1")")

  if [ "$del_code" = "204" ]; then
    # DELETE 先提交：发布不得成功，也不得留下快照。
    seen[B_delete_first]=1
    [ "$pub_code" = "404" ] && [ "$(err_code "$pub_body")" = "RECIPE_VERSION_NOT_FOUND" ] \
      || fail "B#$round DELETE 204 但 publish=$pub_code $(err_code "$pub_body")（应 404 RECIPE_VERSION_NOT_FOUND）"
    [ "$get_code" = "404" ] || fail "B#$round DELETE 后版本仍存在（GET=$get_code，应 404，不得留快照）"
  elif [ "$del_code" = "409" ] && [ "$(err_code "$del_body")" = "RECIPE_VERSION_IMMUTABLE" ]; then
    # 发布先提交：旧 DELETE 必须 409，已发布行及其内容不变。
    seen[B_publish_first]=1
    [ "$pub_code" = "200" ] || fail "B#$round DELETE 409 但 publish=$pub_code（应 200）"
    local final final_status final_qty
    final=$(body_of "$(http GET "/recipes/$code/versions/1")")
    final_status=$(field "['status']" "$final")
    final_qty=$(field "['items'][0]['quantity']" "$final")
    [ "$final_status" = "published" ] || fail "B#$round 最终状态=$final_status（应 published）"
    [ "$final_qty" = "10.0" ] || fail "B#$round 已发布内容=$final_qty g（应保持 10 g）"
  else
    fail "B#$round DELETE 返回 $del_code $(err_code "$del_body")（应 204 或 409 RECIPE_VERSION_IMMUTABLE）"
  fi
}

race_put_vs_delete() { # 竞速 C
  local round="$1" code="race-c-$round-$$"
  mk_recipe "$code"

  local put_out del_out
  put_out=$(mktemp); del_out=$(mktemp)
  http PUT "/recipes/$code/versions/1" \
    '{"items":[{"kind":"ingredient","code":"sugar","quantity":99,"unit":"g"}]}' >"$put_out" &
  http DELETE "/recipes/$code/versions/1" >"$del_out" &
  wait

  local put_code put_body del_code
  put_code=$(status_of "$(cat "$put_out")"); put_body=$(body_of "$(cat "$put_out")")
  del_code=$(status_of "$(cat "$del_out")")
  rm -f "$put_out" "$del_out"

  local get_code
  get_code=$(status_of "$(http GET "/recipes/$code/versions/1")")

  # DELETE 总是生效（PUT 不改变草稿状态）；PUT 的胜负由提交顺序决定。
  [ "$del_code" = "204" ] || fail "C#$round DELETE=$del_code（应 204）"
  if [ "$put_code" = "200" ]; then
    seen[C_put_first]=1
  elif [ "$put_code" = "404" ] && [ "$(err_code "$put_body")" = "RECIPE_VERSION_NOT_FOUND" ]; then
    seen[C_delete_first]=1
  else
    fail "C#$round PUT 返回 $put_code $(err_code "$put_body")（应 200 或 404 RECIPE_VERSION_NOT_FOUND）"
  fi
  [ "$get_code" = "404" ] || fail "C#$round DELETE 后版本仍存在（GET=$get_code，应 404）"
}

check_health
say "并发复验：$BASE，每组竞速 $ROUNDS 轮"
for round in $(seq 1 "$ROUNDS"); do
  race_put_vs_publish "$round"
  race_delete_vs_publish "$round"
  race_put_vs_delete "$round"
  say "  round $round 完成"
done

say ""
say "观察到的提交顺序：${!seen[*]}"
if [ "$failures" -eq 0 ]; then
  say "OK：$((ROUNDS * 3)) 组竞速全部满足原子胜负顺序（已发布版本未被改写或删除）"
  exit 0
else
  say "FAILED：$failures 处不变量被破坏"
  exit 1
fi
