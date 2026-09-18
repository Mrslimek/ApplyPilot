// ApplyPilot Telegram control bot.
//
// Runs on the VPS as a systemd service. Long-polls Telegram (no webhook),
// spawns pipeline commands as detached processes, reports back when done.
// Pure standard library — single static binary.
//
// Setup:
//   1. Create a bot with @BotFather, put TELEGRAM_BOT_TOKEN in ~/.applypilot/.env
//   2. systemctl restart applypilot-bot
//   3. Send "/start <TELEGRAM_SETUP_SECRET>" from your account — locks the bot
//      to your chat id.
//
// Commands:
//   /run        — refresh cycle (discover > enrich > score > tailor > cover)
//   /status     — pipeline statistics
//   /queue      — jobs ready for auto-apply
//   /apply N    — submit up to N applications (send twice within 60s to confirm)
//   /log        — tail of the cycle log
//   /cancel     — kill running cycle/apply/chrome
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var (
	appDir   = envOr("APPLYPILOT_DIR", os.ExpandEnv("$HOME/.applypilot"))
	envFile  = filepath.Join(appDir, ".env")
	dbLocked = false
)

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// -- config -------------------------------------------------------------------

type config struct {
	Token  string
	Owner  string
	Secret string
}

func parseEnvFile(path string) map[string]string {
	out := map[string]string{}
	b, err := os.ReadFile(path)
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if k, v, ok := strings.Cut(line, "="); ok {
			out[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return out
}

// -- paths --------------------------------------------------------------------

func projectDir() string {
	exe, err := os.Executable()
	if err != nil {
		return "/root/ApplyPilot"
	}
	return filepath.Dir(filepath.Dir(exe)) // <project>/bot/binary -> <project>
}

func applypilotBin() string { return filepath.Join(projectDir(), ".venv", "bin", "applypilot") }
func boardsBin() string {
	p := filepath.Join(projectDir(), "boards", "applypilot-boards-linux-amd64")
	if _, err := os.Stat(p); err == nil {
		return p
	}
	return filepath.Join(projectDir(), "boards", "applypilot-boards")
}
func runCycleSh() string    { return filepath.Join(projectDir(), "run_cycle.sh") }
func queuePy() string       { return filepath.Join(projectDir(), "bot", "queue.py") }
func venvPython() string    { return filepath.Join(projectDir(), ".venv", "bin", "python") }

// -- telegram api -------------------------------------------------------------

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*m`)

func send(cfg config, chatID int64, text string) {
	text = ansiRe.ReplaceAllString(text, "")
	if len(text) > 3800 {
		text = text[:3800]
	}
	if strings.TrimSpace(text) == "" {
		text = "(empty)"
	}
	body, _ := json.Marshal(map[string]any{
		"chat_id": chatID, "text": text, "parse_mode": "HTML",
	})
	resp, err := http.Post("https://api.telegram.org/bot"+cfg.Token+"/sendMessage",
		"application/json", strings.NewReader(string(body)))
	if err != nil {
		fmt.Println("send error:", err)
		return
	}
	resp.Body.Close()
}

// -- process management -------------------------------------------------------

type procTracker struct {
	mu    sync.Mutex
	procs map[string]*exec.Cmd
}

var tracker = &procTracker{procs: map[string]*exec.Cmd{}}

func (t *procTracker) running(name string) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	c, ok := t.procs[name]
	return ok && c.Process != nil && c.ProcessState == nil
}

func (t *procTracker) set(name string, c *exec.Cmd) {
	t.mu.Lock()
	t.procs[name] = c
	t.mu.Unlock()
}

func (t *procTracker) drop(name string) {
	t.mu.Lock()
	delete(t.procs, name)
	t.mu.Unlock()
}

// spawnDetached starts cmd in its own session, output appended to logPath.
func spawnDetached(name string, args []string, logPath string) (*exec.Cmd, error) {
	var out *os.File
	var err error
	if logPath != "" {
		_ = os.MkdirAll(filepath.Dir(logPath), 0o755)
		out, err = os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
		if err != nil {
			return nil, err
		}
	} else {
		out, _ = os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	}
	defer out.Close()

	cmd := exec.Command(args[0], args[1:]...)
	cmd.Stdout = out
	cmd.Stderr = out
	cmd.Stdin = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	cmd.Env = append(os.Environ(), "HOME="+os.Getenv("HOME"))
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	tracker.set(name, cmd)
	return cmd, nil
}

// watchAndReport waits for the process, then sends a status summary.
func watchAndReport(cfg config, chatID int64, name, logPath string) {
	cmd := func() *exec.Cmd {
		tracker.mu.Lock()
		defer tracker.mu.Unlock()
		return tracker.procs[name]
	}()
	if cmd == nil {
		return
	}
	_ = cmd.Wait()
	tracker.drop(name)

	summary := runCmd(60, applypilotBin(), "status")
	tail := tailFile(filepath.Join(appDir, "logs", "cron.log"), 8)
	msg := fmt.Sprintf("<b>%s finished</b>\n<pre>%s</pre>", name, clip(summary, 1500))
	if tail != "" {
		msg += "\n<b>log tail:</b>\n<pre>" + tail + "</pre>"
	}
	send(cfg, chatID, msg)
}

func runCmd(timeoutSec int, args ...string) string {
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Env = append(os.Environ(), "HOME="+os.Getenv("HOME"))
	done := make(chan string, 1)
	go func() {
		out, err := cmd.CombinedOutput()
		if err != nil && len(out) == 0 {
			done <- fmt.Sprintf("error: %v", err)
			return
		}
		done <- string(out)
	}()
	select {
	case out := <-done:
		return out
	case <-time.After(time.Duration(timeoutSec) * time.Second):
		_ = cmd.Process.Kill()
		return "timeout"
	}
}

func tailFile(path string, n int) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return clip(strings.Join(lines, "\n"), 1200)
}

func clip(s string, n int) string {
	s = ansiRe.ReplaceAllString(s, "")
	if len(s) > n {
		s = s[:n]
	}
	return s
}

// -- commands -----------------------------------------------------------------

var helpText = strings.Join([]string{
	"ApplyPilot control",
	"/run — refresh cycle (discover→cover)",
	"/boards — harvest remote/ATS job boards now",
	"/status — pipeline stats",
	"/queue — ready for auto-apply",
	"/apply N — submit N applications (confirm by repeat)",
	"/log — tail cycle log",
	"/cancel — stop everything",
}, "\n")

type applyConfirm struct {
	mu   sync.Mutex
	last map[int64]applyReq
}

type applyReq struct {
	n  int
	ts time.Time
}

var confirms = &applyConfirm{last: map[int64]applyReq{}}

func cmdRun(cfg config, chatID int64) string {
	if tracker.running("cycle") {
		return "cycle already running — /cancel to stop it"
	}
	cmd, err := spawnDetached("cycle", []string{runCycleSh()},
		filepath.Join(appDir, "logs", "cron.log"))
	if err != nil {
		return "failed to start: " + err.Error()
	}
	go watchAndReport(cfg, chatID, "cycle", "")
	return fmt.Sprintf("cycle started (pid %d) — I'll message you when it finishes", cmd.Process.Pid)
}

func cmdApply(cfg config, chatID, userID int64, n int) string {
	if tracker.running("apply") {
		return "apply already running — /cancel to stop it"
	}
	confirms.mu.Lock()
	prev, seen := confirms.last[userID]
	confirms.last[userID] = applyReq{n: n, ts: time.Now()}
	confirms.mu.Unlock()

	if !seen || time.Since(prev.ts) > 60*time.Second || prev.n != n {
		return fmt.Sprintf("⚠️ This will submit up to <b>%d</b> real job applications.\n"+
			"Send <code>/apply %d</code> again within 60s to confirm.", n, n)
	}
	deleteConfirm(userID)
	cmd, err := spawnDetached("apply",
		[]string{applypilotBin(), "apply", "--limit", strconv.Itoa(n)},
		filepath.Join(appDir, "logs", "apply_bot.log"))
	if err != nil {
		return "failed to start: " + err.Error()
	}
	go watchAndReport(cfg, chatID, "apply", "")
	return fmt.Sprintf("apply started (pid %d) — I'll report when it finishes", cmd.Process.Pid)
}

func deleteConfirm(userID int64) {
	confirms.mu.Lock()
	delete(confirms.last, userID)
	confirms.mu.Unlock()
}

func cmdQueue() string {
	out := runCmd(30, venvPython(), queuePy())
	if strings.TrimSpace(out) == "" {
		return "queue is empty — run /run to refresh the pool"
	}
	return "<b>Ready for auto-apply:</b>\n<pre>" + clip(out, 3000) + "</pre>"
}

func cmdCancel() string {
	for _, pat := range []string{`run_cycle\.sh`, `applypilot (run|apply)`} {
		_ = exec.Command("pkill", "-f", pat).Run()
	}
	killed := []string{}
	tracker.mu.Lock()
	for name, c := range tracker.procs {
		if c.Process != nil && c.ProcessState == nil {
			_ = syscall.Kill(-c.Process.Pid, syscall.SIGTERM)
			killed = append(killed, name)
		}
		delete(tracker.procs, name)
	}
	tracker.mu.Unlock()
	_ = exec.Command("pkill", "-f", "chrome-workers").Run()
	if len(killed) == 0 {
		killed = append(killed, "(nothing was running)")
	}
	return "killed: " + strings.Join(killed, ", ") + " + chrome"
}

// -- update handling ------------------------------------------------------------

type tgMessage struct {
	Message struct {
		Chat struct {
			ID int64 `json:"id"`
		} `json:"chat"`
		From struct {
			ID int64 `json:"id"`
		} `json:"from"`
		Text string `json:"text"`
	} `json:"message"`
	UpdateID int64 `json:"update_id"`
}

func handle(cfg *config, msg tgMessage) {
	chatID := msg.Message.Chat.ID
	userID := msg.Message.From.ID
	text := strings.TrimSpace(msg.Message.Text)

	if cfg.Owner == "" {
		parts := strings.Fields(text)
		if len(parts) == 2 && parts[0] == "/start" && cfg.Secret != "" && parts[1] == cfg.Secret {
			f, err := os.OpenFile(envFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
			if err == nil {
				fmt.Fprintf(f, "TELEGRAM_CHAT_ID=%d\n", chatID)
				f.Close()
			}
			cfg.Owner = strconv.FormatInt(chatID, 10)
			send(*cfg, chatID, "✅ Owner locked to this chat.\n\n"+helpText)
		} else {
			send(*cfg, chatID, "Bot is not activated. Send /start <setup-secret>.")
		}
		return
	}

	if strconv.FormatInt(chatID, 10) != cfg.Owner {
		return // silently ignore strangers
	}

	parts := strings.Fields(text)
	cmd := parts[0]
	if i := strings.IndexByte(cmd, '@'); i > 0 {
		cmd = cmd[:i] // strip @botname
	}
	arg := ""
	if len(parts) > 1 {
		arg = parts[1]
	}

	switch {
	case cmd == "/start", cmd == "/help":
		send(*cfg, chatID, helpText)
	case cmd == "/run":
		send(*cfg, chatID, cmdRun(*cfg, chatID))
	case cmd == "/boards":
		send(*cfg, chatID, "⏳ harvesting job boards (1-5 min) — I'll send the report when done")
		go func() {
			out := runCmd(360, boardsBin())
			send(*cfg, chatID, "<pre>"+clip(out, 3500)+"</pre>")
		}()
	case cmd == "/status":
		send(*cfg, chatID, "<pre>"+clip(runCmd(60, applypilotBin(), "status"), 3500)+"</pre>")
	case cmd == "/queue":
		send(*cfg, chatID, cmdQueue())
	case cmd == "/apply":
		n := 1
		if v, err := strconv.Atoi(arg); err == nil {
			if v < 1 {
				v = 1
			}
			if v > 10 {
				v = 10
			}
			n = v
		}
		send(*cfg, chatID, cmdApply(*cfg, chatID, userID, n))
	case cmd == "/log":
		send(*cfg, chatID, "<pre>"+tailFile(filepath.Join(appDir, "logs", "cron.log"), 30)+"</pre>")
	case cmd == "/cancel":
		send(*cfg, chatID, cmdCancel())
	default:
		send(*cfg, chatID, "unknown command — /help")
	}
}

func main() {
	cfg := &config{}
	loaded := parseEnvFile(envFile)
	cfg.Token = loaded["TELEGRAM_BOT_TOKEN"]
	cfg.Owner = loaded["TELEGRAM_CHAT_ID"]
	cfg.Secret = loaded["TELEGRAM_SETUP_SECRET"]

	if cfg.Token == "" {
		fmt.Println("TELEGRAM_BOT_TOKEN not set in ~/.applypilot/.env — exiting")
		os.Exit(1)
	}
	fmt.Printf("bot up | owner=%s\n", map[bool]string{true: "set", false: "not set"}[cfg.Owner != ""])

	var offset int64
	client := &http.Client{Timeout: 70 * time.Second}
	for {
		url := fmt.Sprintf("https://api.telegram.org/bot%s/getUpdates?timeout=50&offset=%d&allowed_updates=%s",
			cfg.Token, offset, `%5B%22message%22%5D`)
		resp, err := client.Get(url)
		if err != nil {
			fmt.Println("poll error:", err)
			time.Sleep(5 * time.Second)
			continue
		}
		var payload struct {
			Result []tgMessage `json:"result"`
		}
		dec := json.NewDecoder(resp.Body)
		_ = dec.Decode(&payload)
		resp.Body.Close()

		for _, upd := range payload.Result {
			offset = upd.UpdateID + 1
			if upd.Message.Chat.ID != 0 {
				handle(cfg, upd)
			}
		}
	}
}
