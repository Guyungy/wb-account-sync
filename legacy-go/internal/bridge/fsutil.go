package bridge

import (
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
)

// RequireSpace 检查目标卷剩余空间。本机内部数据卷极易被打满，宁可提前拒绝。
//
// 与 Python 版一致地"向上找最近的存在路径"：用户给的 dest 可能还没建出来，
// 直接 stat 它会拿到 ENOENT，但空间够不够这件事跟路径存不存在无关。
func RequireSpace(path string, needed int64, what string, headroom int64) error {
	probe := path
	for probe != "" {
		if _, err := os.Stat(probe); err == nil {
			break
		}
		parent := filepath.Dir(probe)
		if parent == probe {
			probe = ""
			break
		}
		probe = parent
	}
	if probe == "" {
		return nil
	}
	var st syscall.Statfs_t
	if err := syscall.Statfs(probe, &st); err != nil {
		return nil // 探测失败不阻断：与 Python 的 disk_usage 抛错后再判断保持同一取向
	}
	free := int64(st.Bavail) * int64(st.Bsize)
	if free < needed+headroom {
		return errf("磁盘空间不足，拒绝%s。\n"+
			"  所在卷     : %s\n"+
			"  需要约     : %s（另需 %s 余量）\n"+
			"  当前可用   : %s\n"+
			"请先清理空间，或改用 --no-changes 减少体积。",
			what, probe, Human(float64(needed)), Human(float64(headroom)), Human(float64(free)))
	}
	return nil
}

// StateDirGuard 保证 state-dir 是 0700 的、归属本工具的目录。
//
// 这条检查是为了拦住"误把家目录或某个共享目录当 state-dir"——
// 里面有内容且权限不是 0700 就说明它已经名花有主，不该拿来放 undo journal。
func StateDirGuard(path string) (string, error) {
	path = platform.ExpandPath(path)
	info, err := os.Stat(path)
	if err == nil {
		if !info.IsDir() {
			return "", errf("--state-dir 不是目录：%s", path)
		}
		entries, ierr := os.ReadDir(path)
		if ierr != nil {
			return "", ierr
		}
		if len(entries) > 0 && info.Mode().Perm() != 0o700 {
			return "", errf("--state-dir 已存在且有内容，但权限不是 0700：%s\n"+
				"请提供一个新目录，或使用归属本工具的 0700 目录。", path)
		}
		return path, nil
	}
	if err := os.MkdirAll(path, 0o700); err != nil {
		return "", err
	}
	return path, nil
}

// ExpandPath 是 platform.ExpandPath 的本地别名，路径展开规则只维护一份。
func ExpandPath(p string) string { return platform.ExpandPath(p) }

// --------------------------------------------------------------------------
// 复制原语
// --------------------------------------------------------------------------

// copy2 等价于 shutil.copy2：拷内容 + 权限位 + 时间戳。
//
// 只拷内容是不够的：blobs 与 artifact 目录靠 mtime 参与增量判断，
// 权限位丢了还会让后续 osascript 之类的调用行为漂移。
func copy2(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	info, err := in.Stat()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, info.Mode().Perm())
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	// 权限位显式落一次：O_CREATE 给的 mode 会被 umask 削掉。
	if err := os.Chmod(dst, info.Mode().Perm()); err != nil {
		return err
	}
	return os.Chtimes(dst, info.ModTime(), info.ModTime())
}

// CopyFile 返回 "skip" / "create" / "overwrite"，与 Python 侧同一套状态词。
func CopyFile(src, dst string, overwrite bool) (string, error) {
	_, statErr := os.Stat(dst)
	existed := statErr == nil
	if existed && !overwrite {
		return "skip", nil
	}
	if err := copy2(src, dst); err != nil {
		return "", err
	}
	if existed {
		return "overwrite", nil
	}
	return "create", nil
}

// MergeTree 把 src 并进 dst，已存在的文件默认跳过。
//
// 返回 (created, skipped)。单个文件失败不中止整体迁移：日志资产里
// 常见权限古怪或是 socket 的条目，为一个文件放弃整批不值得。
func MergeTree(src, dst string, overwrite bool, warn func(string)) (int, int, error) {
	created, skipped := 0, 0
	err := filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, rerr := filepath.Rel(src, path)
		if rerr != nil {
			return rerr
		}
		target := dst
		if rel != "." {
			target = filepath.Join(dst, rel)
		}
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if d.Type()&fs.ModeSymlink != 0 {
			// 与 copytree(symlinks=True) 一致：原样重建链接，不跟随拷贝。
			link, lerr := os.Readlink(path)
			if lerr != nil {
				warn(fmt.Sprintf("  ! 跳过 %s：%v", path, lerr))
				skipped++
				return nil
			}
			if _, serr := os.Lstat(target); serr == nil && !overwrite {
				skipped++
				return nil
			}
			os.Remove(target)
			if serr := os.Symlink(link, target); serr != nil {
				warn(fmt.Sprintf("  ! 跳过 %s：%v", path, serr))
				skipped++
				return nil
			}
			created++
			return nil
		}
		if _, serr := os.Stat(target); serr == nil && !overwrite {
			skipped++
			return nil
		}
		if cerr := copy2(path, target); cerr != nil {
			warn(fmt.Sprintf("  ! 跳过 %s：%v", path, cerr))
			skipped++
			return nil
		}
		created++
		return nil
	})
	return created, skipped, err
}

// CopyTree 返回 "create" / "merge"。
func CopyTree(src, dst string, overwrite bool, warn func(string)) (string, error) {
	info, err := os.Stat(dst)
	existed := err == nil && info.IsDir()
	if existed && !overwrite {
		if _, _, merr := MergeTree(src, dst, overwrite, warn); merr != nil {
			return "", merr
		}
		return "merge", nil
	}
	if err := CopyTreeFull(src, dst); err != nil {
		return "", err
	}
	return "create", nil
}

// CopyTreeFull 等价于 shutil.copytree(symlinks=True, dirs_exist_ok=True)。
func CopyTreeFull(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, rerr := filepath.Rel(src, path)
		if rerr != nil {
			return rerr
		}
		target := dst
		if rel != "." {
			target = filepath.Join(dst, rel)
		}
		switch {
		case d.IsDir():
			return os.MkdirAll(target, 0o755)
		case d.Type()&fs.ModeSymlink != 0:
			link, lerr := os.Readlink(path)
			if lerr != nil {
				return lerr
			}
			os.Remove(target)
			return os.Symlink(link, target)
		default:
			return copy2(path, target)
		}
	})
}

// --------------------------------------------------------------------------
// 去重与并集
// --------------------------------------------------------------------------

func uniqueStrings(items []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if seen[item] {
			continue
		}
		seen[item] = true
		out = append(out, item)
	}
	return out
}
