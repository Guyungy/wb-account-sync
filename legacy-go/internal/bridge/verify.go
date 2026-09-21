package bridge

import (
	"encoding/json"
	"fmt"
	"os"
)

// VerifyResult 是一侧的核验结果。
type VerifyResult struct {
	Side    string
	Target  string
	Want    int
	Missing []string
	Wrong   []string
	Absent  int
}

// OK 表示这一侧没有发现问题。
func (v VerifyResult) OK() bool {
	return len(v.Missing) == 0 && len(v.Wrong) == 0 && v.Absent == 0
}

// Verify 核验一次执行的落地情况：行在不在、归属对不对、正文文件在不在。
//
// 只读，不改任何数据。**能力边界要说清楚**：它验证的是"本地会话行 +
// 正文文件"，覆盖不到界面展示、云端同步一致性、正文以外的资产、
// 跨 App 的权限模型。别把"核验通过"读成"一切都对"。
func Verify(a, b *Home, planPath string, log func(string)) ([]VerifyResult, bool, error) {
	raw, err := os.ReadFile(planPath)
	if err != nil {
		return nil, false, errf("读取计划失败：%v", err)
	}
	var plan struct {
		Rows map[string]map[string][]map[string]any `json:"rows"`
	}
	if err := json.Unmarshal(raw, &plan); err != nil {
		return nil, false, errf("解析计划失败：%v", err)
	}

	targets := map[string]*Home{"a2b": b, "b2a": a}
	out := []VerifyResult{}
	allOK := true
	for _, side := range []string{"a2b", "b2a"} {
		target := targets[side]
		want := plan.Rows[side]["sessions"]
		res := VerifyResult{Side: side, Target: target.Label, Want: len(want)}

		db, derr := target.Connect()
		if derr != nil {
			return nil, false, derr
		}
		have := map[string]bool{}
		ownerOf := map[string]string{}
		rows, qerr := db.Query("SELECT id, user_id FROM sessions")
		if qerr != nil {
			db.Close()
			return nil, false, errf("[%s] 读取 sessions 失败：%v", target.Label, qerr)
		}
		for rows.Next() {
			var id string
			var owner any
			if serr := rows.Scan(&id, &owner); serr != nil {
				rows.Close()
				db.Close()
				return nil, false, serr
			}
			have[id] = true
			ownerOf[id] = asString(owner)
		}
		rows.Close()
		db.Close()

		for _, row := range want {
			id := asString(row["id"])
			if !have[id] {
				res.Missing = append(res.Missing, id)
				continue
			}
			if ownerOf[id] != asString(row["user_id"]) {
				res.Wrong = append(res.Wrong, id)
			}
		}
		// 正文文件缺失单独计数：数据库有行但没正文，客户端里会是一条打不开的会话。
		for _, row := range want {
			if len(ProjectSlugFor(target, asString(row["id"]))) == 0 {
				res.Absent++
			}
		}

		log(fmt.Sprintf("[%s] → %s: 应有 %d 条 | 缺行 %d | 归属错 %d | 缺正文 %d",
			side, target.Label, res.Want, len(res.Missing), len(res.Wrong), res.Absent))
		if !res.OK() {
			allOK = false
			shown := res.Missing
			if len(shown) == 0 {
				shown = res.Wrong
			}
			for i, id := range shown {
				if i >= 5 {
					break
				}
				log(fmt.Sprintf("    ! %s", id))
			}
		}
		out = append(out, res)
	}

	verdict := "通过（仅就本地会话行与正文文件而言）"
	if !allOK {
		verdict = "未通过"
	}
	log("\n核验结论：" + verdict)
	log("未覆盖：界面展示、云端同步一致性、正文以外的资产、跨 App 的权限模型。")
	return out, allOK, nil
}
