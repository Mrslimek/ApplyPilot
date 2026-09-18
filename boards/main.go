// applypilot-boards: remote job boards harvester for ApplyPilot.
//
// Fetches public job-board APIs (Remotive, RemoteOK, Greenhouse, Lever),
// applies the same filters as the Python pipeline (swamp-location
// blacklist, tombstones, dedup by URL, title keyword net) and inserts
// jobs straight into the shared SQLite database — with application_url
// and full_description already set, so the enrichment stage is skipped.
//
// Board tokens live in ~/.applypilot/boards.yaml (greenhouse: / lever:
// lists). Called by the nightly cycle and by the Telegram bot (/boards).
package main

import (
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// ── config ─────────────────────────────────────────────────────────────

func home(p ...string) string {
	h, err := os.UserHomeDir()
	if err != nil {
		h = "/root"
	}
	return filepath.Join(append([]string{h}, p...)...)
}

// loadYAMLSection extracts a flat `key:` list of strings from a simple
// YAML file. Avoids a YAML dependency: our config sections are plain
// "- item" lists.
func loadYAMLSection(path, key string) []string {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var out []string
	inSection := false
	want := key + ":"
	for _, line := range strings.Split(string(b), "\n") {
		trimmed := strings.TrimSpace(line)
		if !inSection {
			if trimmed == want {
				inSection = true
			}
			continue
		}
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		if !strings.HasPrefix(trimmed, "- ") {
			break // next top-level key reached
		}
		item := strings.Trim(strings.TrimSpace(strings.TrimPrefix(trimmed, "- ")), `"'`)
		if item != "" {
			out = append(out, item)
		}
	}
	return out
}

// default boards when ~/.applypilot/boards.yaml is absent (probed live
// 2026-09; keep in sync via the yaml file on the server)
var defaultGreenhouse = []string{
	"duolingo", "figma", "cloudflare", "anthropic", "discord",
	"datadog", "mongodb", "netlify", "vercel", "planetscale",
	"mixpanel", "monzo", "peloton", "robinhood", "gusto",
	"launchdarkly", "fivetran", "asana",
}
var defaultLever = []string{"mistral", "kraken"}

var titleNet = regexp.MustCompile(`(?i)python|django|fastapi|flask|back[ -]?end`)

func compileSwamp(entries []string) []*regexp.Regexp {
	var out []*regexp.Regexp
	for _, e := range entries {
		e = strings.ToLower(strings.TrimSpace(e))
		if e == "" {
			continue
		}
		// word-boundary match: "india" must not match "Indiana"
		if re, err := regexp.Compile(`\b` + e + `\b`); err == nil {
			out = append(out, re)
		}
	}
	return out
}

func locationOK(loc string, swamp []*regexp.Regexp) bool {
	l := strings.ToLower(loc)
	for _, re := range swamp {
		if re.MatchString(l) {
			return false
		}
	}
	return true
}

var tagStrip = regexp.MustCompile(`<[^>]+>`)
var multiSpace = regexp.MustCompile(`[ \t]+`)

func htmlToText(s string) string {
	s = strings.ReplaceAll(s, "<br>", "\n")
	s = strings.ReplaceAll(s, "<br/>", "\n")
	s = strings.ReplaceAll(s, "<p>", "\n")
	s = strings.ReplaceAll(s, "</p>", "\n")
	s = strings.ReplaceAll(s, "<li>", "\n- ")
	s = tagStrip.ReplaceAllString(s, " ")
	s = strings.NewReplacer("&amp;", "&", "&lt;", "<", "&gt;", ">", "&quot;", `"`, "&#39;", "'", "&nbsp;", " ", "&mdash;", "—", "&ndash;", "–").Replace(s)
	lines := strings.Split(s, "\n")
	for i, l := range lines {
		lines[i] = strings.TrimSpace(multiSpace.ReplaceAllString(l, " "))
	}
	out := strings.Join(lines, "\n")
	for strings.Contains(out, "\n\n\n") {
		out = strings.ReplaceAll(out, "\n\n\n", "\n\n")
	}
	return strings.TrimSpace(out)
}

// ── job model ──────────────────────────────────────────────────────────

type job struct {
	URL         string
	Title       string
	Company     string
	Location    string
	Description string // plain text
	ApplyURL    string
	Strategy    string
}

// ── sources ────────────────────────────────────────────────────────────

var httpClient = &http.Client{Timeout: 40 * time.Second}

func fetchJSON(url string, out any) error {
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", "applypilot-boards/1.0 (personal job-search tool)")
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 30<<20))
	if err != nil {
		return err
	}
	return json.Unmarshal(body, out)
}

type remotiveResp struct {
	Jobs []struct {
		URL      string `json:"url"`
		Title    string `json:"title"`
		Company  string `json:"company_name"`
		Location string `json:"candidate_required_location"`
		Desc     string `json:"description"`
	} `json:"jobs"`
}

func fetchRemotive(limit int) ([]job, error) {
	var r remotiveResp
	if err := fetchJSON(fmt.Sprintf("https://remotive.com/api/remote-jobs?limit=%d", limit), &r); err != nil {
		return nil, err
	}
	out := make([]job, 0, len(r.Jobs))
	for _, j := range r.Jobs {
		if j.URL == "" || j.Title == "" {
			continue
		}
		out = append(out, job{
			URL: j.URL, Title: j.Title, Company: j.Company,
			Location: j.Location, Description: htmlToText(j.Desc),
			ApplyURL: j.URL, // remotive page carries the Apply button -> external ATS
			Strategy: "remotive",
		})
	}
	return out, nil
}

type remoteOKJob struct {
	Position string `json:"position"`
	Company  string `json:"company"`
	URL      string `json:"url"`
	ApplyURL string `json:"apply_url"`
	Loc      string `json:"location"`
	Desc     string `json:"description"`
}

func fetchRemoteOK() ([]job, error) {
	var raw []json.RawMessage
	if err := fetchJSON("https://remoteok.com/api", &raw); err != nil {
		return nil, err
	}
	out := []job{}
	for _, item := range raw {
		var j remoteOKJob
		if err := json.Unmarshal(item, &j); err != nil || j.URL == "" || j.Position == "" {
			continue // legal-notice element and malformed entries
		}
		apply := j.ApplyURL
		if apply == "" {
			apply = j.URL
		}
		out = append(out, job{
			URL: j.URL, Title: j.Position, Company: j.Company,
			Location: j.Loc, Description: htmlToText(j.Desc),
			ApplyURL: apply, Strategy: "remoteok",
		})
	}
	return out, nil
}

type greenhouseResp struct {
	Jobs []struct {
		Title    string `json:"title"`
		AbsURL   string `json:"absolute_url"`
		Location struct {
			Name string `json:"name"`
		} `json:"location"`
		Content string `json:"content"`
	} `json:"jobs"`
}

func fetchGreenhouse(token string) ([]job, error) {
	var r greenhouseResp
	if err := fetchJSON("https://boards-api.greenhouse.io/v1/boards/"+token+"/jobs?content=true", &r); err != nil {
		return nil, err
	}
	company := strings.ToUpper(token[:1]) + token[1:]
	out := make([]job, 0, len(r.Jobs))
	for _, j := range r.Jobs {
		if j.AbsURL == "" || j.Title == "" {
			continue
		}
		out = append(out, job{
			URL: j.AbsURL, Title: j.Title, Company: company,
			Location: j.Location.Name, Description: htmlToText(j.Content),
			ApplyURL: j.AbsURL, Strategy: "greenhouse",
		})
	}
	return out, nil
}

type leverPosting struct {
	Text      string `json:"text"`
	HostedURL string `json:"hostedUrl"`
	ApplyURL  string `json:"applyUrl"`
	Cats      struct {
		Location string `json:"location"`
	} `json:"categories"`
	DescPlain string `json:"descriptionPlain"`
}

func fetchLever(token string) ([]job, error) {
	var r []leverPosting
	if err := fetchJSON("https://api.lever.co/v0/postings/"+token+"?mode=json", &r); err != nil {
		return nil, err
	}
	company := strings.ToUpper(token[:1]) + token[1:]
	out := make([]job, 0, len(r))
	for _, j := range r {
		if j.HostedURL == "" || j.Text == "" {
			continue
		}
		apply := j.ApplyURL
		if apply == "" {
			apply = j.HostedURL
		}
		out = append(out, job{
			URL: j.HostedURL, Title: j.Text, Company: company,
			Location: j.Cats.Location, Description: j.DescPlain,
			ApplyURL: apply, Strategy: "lever",
		})
	}
	return out, nil
}

// ── main ───────────────────────────────────────────────────────────────

func main() {
	dbPath := flag.String("db", home(".applypilot", "applypilot.db"), "path to applypilot.db")
	cfgPath := flag.String("config", home(".applypilot", "searches.yaml"), "searches.yaml for the swamp list")
	boardsPath := flag.String("boards", home(".applypilot", "boards.yaml"), "boards.yaml with greenhouse/lever tokens")
	limit := flag.Int("limit", 200, "max jobs per aggregator source")
	flag.Parse()

	swamp := compileSwamp(loadYAMLSection(*cfgPath, "location_reject"))

	ghTokens := loadYAMLSection(*boardsPath, "greenhouse")
	if len(ghTokens) == 0 {
		ghTokens = defaultGreenhouse
	}
	lvTokens := loadYAMLSection(*boardsPath, "lever")
	if len(lvTokens) == 0 {
		lvTokens = defaultLever
	}

	db, err := sql.Open("sqlite", *dbPath+"?_pragma=busy_timeout(15000)&_pragma=journal_mode(WAL)")
	if err != nil {
		fmt.Println("open db:", err)
		os.Exit(1)
	}
	defer db.Close()

	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS dismissed_urls (
		url TEXT PRIMARY KEY, fit_score INTEGER, dismissed_at TEXT)`); err != nil {
		fmt.Println("ensure dismissed table:", err)
		os.Exit(1)
	}

	dismissed := map[string]bool{}
	rows, err := db.Query("SELECT url FROM dismissed_urls")
	if err == nil {
		for rows.Next() {
			var u string
			_ = rows.Scan(&u)
			dismissed[u] = true
		}
		rows.Close()
	}

	sources := []struct {
		name string
		fn   func() ([]job, error)
	}{
		{"remotive", func() ([]job, error) { return fetchRemotive(*limit) }},
		{"remoteok", fetchRemoteOK},
	}
	for _, t := range ghTokens {
		tok := t
		sources = append(sources, struct {
			name string
			fn   func() ([]job, error)
		}{"greenhouse/" + tok, func() ([]job, error) { return fetchGreenhouse(tok) }})
	}
	for _, t := range lvTokens {
		tok := t
		sources = append(sources, struct {
			name string
			fn   func() ([]job, error)
		}{"lever/" + tok, func() ([]job, error) { return fetchLever(tok) }})
	}

	totalNew := 0
	for _, src := range sources {
		jobs, err := src.fn()
		if err != nil {
			fmt.Printf("[boards] %s: FETCH ERROR: %v\n", src.name, err)
			continue
		}
		fetched, kept, filteredTitle, filteredLoc, dupes, dismissedN, inserted := len(jobs), 0, 0, 0, 0, 0, 0
		now := time.Now().UTC().Format(time.RFC3339)
		for _, j := range jobs {
			if !titleNet.MatchString(j.Title) {
				filteredTitle++
				continue
			}
			if !locationOK(j.Location, swamp) {
				filteredLoc++
				continue
			}
			if dismissed[j.URL] {
				dismissedN++
				continue
			}
			kept++
			res, err := db.Exec(
				`INSERT OR IGNORE INTO jobs
				 (url, title, description, location, site, strategy, discovered_at,
				  full_description, application_url, detail_scraped_at)
				 VALUES (?,?,?,?,?,?,?,?,?,?)`,
				j.URL, j.Title, j.Description, j.Location, j.Company, j.Strategy, now,
				j.Description, j.ApplyURL, now)
			if err != nil {
				fmt.Printf("[boards] %s: insert error: %v\n", src.name, err)
				continue
			}
			if n, _ := res.RowsAffected(); n > 0 {
				inserted++
				totalNew++
			} else {
				dupes++
			}
		}
		fmt.Printf("[boards] %s: fetched %d | kept %d | new %d | dupes %d | filtered title %d, location %d | tombstoned %d\n",
			src.name, fetched, kept, inserted, dupes, filteredTitle, filteredLoc, dismissedN)
	}
	fmt.Printf("[boards] total new jobs: %d\n", totalNew)
}
