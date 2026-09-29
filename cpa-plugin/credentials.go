package main

import (
	"encoding/json"
	"sort"
	"strings"
	"time"
)

type authEntry struct {
	ID             string    `json:"id"`
	Index          string    `json:"auth_index"`
	Provider       string    `json:"provider"`
	Type           string    `json:"type"`
	Status         string    `json:"status"`
	Disabled       bool      `json:"disabled"`
	Unavailable    bool      `json:"unavailable"`
	RuntimeOnly    bool      `json:"runtime_only"`
	Priority       int       `json:"priority"`
	NextRetryAfter time.Time `json:"next_retry_after"`
}

func (a authEntry) eligible(now time.Time) bool {
	provider := a.Provider
	if provider == "" {
		provider = a.Type
	}
	return strings.EqualFold(provider, "codex") && a.Index != "" &&
		!a.Disabled && !a.Unavailable && !a.RuntimeOnly &&
		(a.Status == "" || a.Status == "active") && !a.NextRetryAfter.After(now)
}

// Read through the host on every execution. Do not cache access/refresh tokens,
// copy auth files, or claim the native Codex authentication provider.
func (p *Plugin) selectSession(callback string) (session, string, error) {
	var listing struct {
		Files []authEntry `json:"files"`
	}
	if err := p.host("host.auth.list", object{"host_callback_id": callback}, &listing); err != nil {
		return session{}, "", fail(503, "CPA Codex credential listing is unavailable")
	}
	candidates := []authEntry{}
	for _, entry := range listing.Files {
		if entry.eligible(time.Now()) {
			candidates = append(candidates, entry)
		}
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].Priority != candidates[j].Priority {
			return candidates[i].Priority > candidates[j].Priority
		}
		return candidates[i].Index < candidates[j].Index
	})
	if len(candidates) == 0 {
		return session{}, "", fail(503, "no active file-backed Codex OAuth credential in CPA; use CPA's normal Codex login/import")
	}
	p.mu.Lock()
	turn := p.authTurn
	p.authTurn++
	p.mu.Unlock()
	lastErr := fail(503, "no readable, active Codex credential is available")
	for start := 0; start < len(candidates); {
		end := start + 1
		for end < len(candidates) && candidates[end].Priority == candidates[start].Priority {
			end++
		}
		for n := 0; n < end-start; n++ {
			entry := candidates[start+(int(turn%uint64(end-start))+n)%(end-start)]
			var runtime struct {
				Auth authEntry `json:"auth"`
			}
			if err := p.host("host.auth.get_runtime", object{"host_callback_id": callback, "auth_index": entry.Index}, &runtime); err != nil {
				continue
			}
			if runtime.Auth.Index != entry.Index || !runtime.Auth.eligible(time.Now()) {
				continue
			}
			var stored struct {
				JSON json.RawMessage `json:"json"`
			}
			if err := p.host("host.auth.get", object{"host_callback_id": callback, "auth_index": entry.Index}, &stored); err != nil {
				continue
			}
			var data object
			if json.Unmarshal(stored.JSON, &data) != nil || !strings.EqualFold(str(data["type"]), "codex") || data["disabled"] == true {
				continue
			}
			// The direct-executor HTTP callback uses the host-global proxy. Do not
			// silently bypass a credential's explicit network policy.
			if strings.TrimSpace(str(data["proxy_url"])) != "" {
				lastErr = fail(503, "per-credential proxy_url is not supported by this CPA callback; configure CPA's global proxy for Excel mode")
				continue
			}
			sess, err := sessionFromJSON(stored.JSON, true)
			if err != nil {
				lastErr = err
				continue
			}
			return sess, entry.Index, nil
		}
		start = end
	}
	return session{}, "", lastErr
}
