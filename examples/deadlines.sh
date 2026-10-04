#!/usr/bin/env bash
# 本轮用法端到端示例: 当前发布版的统一 UTC 评审截止时刻
#   发布 -> 查看槽位状态 -> 设置统一 UTC 截止 (双版本核对) ->
#   同版同值重试幂等 / 同版异值拒绝 / 版本不符拒绝 ->
#   截止后未交槽位逾期 (不得再确认/交评语, 已交评语及收据有效) ->
#   补位预演/发布释放逾期槽位且不把该稿重分给逾期评审人 ->
#   新发布序号不继承旧截止
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/deadlines.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
rh() { echo "X-Reviewer-Id: $1"; echo "X-Reviewer-Credential: $2"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文, 并发布 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'

REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
echo "$PUB"
SERIAL=$(echo "$PUB" | j 'd["serial"]')
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
echo "发布序号 serial=$SERIAL, 槽位评审人: $RA / $RB"

echo "== 2. 未设置截止时查看槽位状态 (沿用现有行为, 无 overdue) =="
curl -s "$BASE/assignment/review-deadline" -H "$ORG"; echo

echo "== 3. R1 确认但暂不交评语; R2 确认并按时交评语 (该槽位截止后保持 submitted) =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "$(rh R1 r1-secret)" -H 'Content-Type: application/json' \
  -d '{"paper_id":"P1","decision":"confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "$(rh R2 r2-secret)" -H 'Content-Type: application/json' \
  -d '{"paper_id":"P1","decision":"confirm"}'
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "$(rh R2 r2-secret)" -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$SERIAL,\"score\":5,\"comment\":\"按时提交的评语\"}"; echo

echo "== 4. 会务方为当前发布版设置统一 UTC 评审截止时刻 (须携带修订号+发布序号) =="
# 截止时刻取当前时刻约 2 小时后 (UTC, 以 Z 结尾; 必须晚于设置时刻)
DEADLINE=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=2)).isoformat().replace("+00:00","Z"))')
echo "deadline_at = $DEADLINE"
curl -s -X POST "$BASE/assignment/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"$DEADLINE\"}"; echo

echo "== 5. 同版同值重试 -> changed=false (不产生新变更); 同版异值 -> 409 =="
curl -s -X POST "$BASE/assignment/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"$DEADLINE\"}" \
  | j '{changed: d["changed"], deadline_at: d["review_deadline"]["deadline_at"]}'
LATER=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=3)).isoformat().replace("+00:00","Z"))')
curl -s -o /dev/null -w '同版异值 -> HTTP %{http_code}\n' \
  -X POST "$BASE/assignment/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"$LATER\"}"
echo "非法时刻演示: 无时区 -> 422; 过去时刻 -> 422; 错误修订号/序号 -> 409"
curl -s -o /dev/null -w '无时区       -> HTTP %{http_code}\n' \
  -X POST "$BASE/assignment/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SERIAL, \"deadline_at\": \"2099-01-01T00:00:00\"}"
curl -s -o /dev/null -w '错误发布序号 -> HTTP %{http_code}\n' \
  -X POST "$BASE/assignment/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $((SERIAL+1)), \"deadline_at\": \"$DEADLINE\"}"

echo "== 6. (说明) 到达截止时刻后, 未交评语的槽位即逾期 =="
echo '此时 GET /assignment/review-deadline 的 summary 中 overdue 增加; 逾期槽位:'
echo '  - POST /reviewer/assignments/P1/decision {"decision":"confirm"} -> 409 (已逾期不得确认)'
echo '  - POST /reviewer/assignments/P1/review {...}                       -> 409 (已逾期不得交评语)'
echo '  - R2 已交评语: 相同内容重试仍返回原收据 (changed=false), 收据与评语继续有效'
echo
echo '可在数据库里把截止行改为过去时刻后复现 (服务端以当前时刻实时判定, 无需重启):'
echo "  sqlite3 \$DB_PATH \"UPDATE review_deadlines SET deadline_at=datetime('now','-1 minute')||':00+00:00' WHERE serial=$SERIAL;\""

echo "== 7. 补位预演: 释放逾期槽位 (即使此前已确认), 且不把 P1 重分给逾期评审人 =="
echo 'POST /assignment/backfill/dry-run 返回中:'
echo '  released_overdue_slots: 本次释放的逾期槽位'
echo '  papers.P1.excluded: 逾期评审人原因 fixed_reviewer_review_overdue'
echo '  plan.P1: 不含逾期评审人 (良好已确认槽位仍固定, 已交评语沿用)'
echo
echo '补位发布 (双版本复核) 后:'
echo '  POST /assignment/backfill/publish {"base_revision":N,"base_serial":S}'
echo '  -> serial+1, released_overdue_slots 计数, carried_reviews 沿用已交评语'

echo "== 8. 旧版截止不约束新发布版 =="
echo "重新普通发布 (POST /assignment/publish) 推进到 serial=$((SERIAL+1)) 后:"
echo '  GET /assignment/review-deadline -> review_deadline=null (新序号未设置截止),'
echo '  deadline_history 中仍保留 serial='$SERIAL' 的旧截止行仅供追溯;'
echo '  新序号需另行设置截止, 未设置期间确认/交评语不受旧截止约束。'
