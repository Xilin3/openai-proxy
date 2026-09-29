package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strings"
	"time"
)

type session struct{ token, account, user string }

func sessionFromJSON(raw []byte, checkExpiry bool) (session, error) {
	var data object
	if json.Unmarshal(raw, &data) != nil {
		return session{}, fail(401, "invalid BPS credential JSON")
	}
	tokens := obj(data["tokens"])
	if tokens == nil {
		tokens = data
	}
	s := session{token: str(tokens["access_token"]), account: str(tokens["account_id"])}
	parts := strings.Split(s.token, ".")
	if len(parts) != 3 {
		return s, fail(401, "BPS credential requires a JWT access_token")
	}
	payload, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return s, fail(401, "invalid access token claims")
	}
	var claims object
	if json.Unmarshal(payload, &claims) != nil {
		return s, fail(401, "invalid access token claims")
	}
	auth := obj(claims["https://api.openai.com/auth"])
	if account := str(auth["chatgpt_account_id"]); account != "" {
		s.account = account
	}
	s.user = str(auth["chatgpt_account_user_id"])
	if s.account == "" {
		return s, fail(401, "BPS credential requires account_id")
	}
	exp, _ := claims["exp"].(float64)
	if checkExpiry && int64(exp) <= time.Now().Unix()+30 {
		return s, fail(401, "CPA Codex access token expired; wait for CPA's native refresh or re-login through CPA")
	}
	return s, nil
}

func (s session) headers() http.Header {
	h := http.Header{}
	values := map[string]string{
		"authorization": "Bearer " + s.token, "chatgpt-account-id": s.account, "x-openai-account-id": s.account,
		"content-type": "application/json", "accept": "text/event-stream", "accept-encoding": "identity",
		"origin":                  "https://bps.openai.com",
		"user-agent":              "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
		"x-basispoints-auth-mode": "chatgpt",
		"x-openai-internal-basispoints-client-agent-profile": "excel",
		"x-openai-internal-basispoints-client-editor":        "excel", "x-openai-internal-basispoints-client-host": "office",
		"x-openai-internal-basispoints-client-platform": "excel", "x-openai-internal-basispoints-client-platform-class": "PC",
		"x-openai-internal-basispoints-client-product": "basispoints-excel-plugin",
		"x-openai-internal-basispoints-client-runtime": "desktop", "x-openai-internal-basispoints-office-host": "Excel",
		"x-openai-internal-basispoints-office-platform": "PC",
		"x-stainless-arch": "unknown", "x-stainless-lang": "js", "x-stainless-os": "Unknown",
		"x-stainless-package-version": "6.31.0", "x-stainless-retry-count": "0", "x-stainless-runtime": "browser:chrome",
	}
	if s.user != "" {
		values["x-openai-account-user-id"] = s.user
	}
	for k, v := range values {
		h.Set(k, v)
	}
	return h
}
