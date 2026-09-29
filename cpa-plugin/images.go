package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"image"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"mime/multipart"
	"net/textproto"
	"strings"

	_ "golang.org/x/image/webp"
)

func imageParts(body object) []object {
	parts := []object{}
	for _, v := range arr(body["input"]) {
		item := obj(v)
		field := "content"
		if item["type"] == "function_call_output" || item["type"] == "custom_tool_call_output" {
			field = "output"
		}
		for _, p := range arr(item[field]) {
			m := obj(p)
			if m["type"] == "input_image" {
				parts = append(parts, m)
			}
		}
	}
	return parts
}
func imageURL(part object) string {
	if u, ok := part["image_url"].(string); ok {
		return u
	}
	return str(obj(part["image_url"])["url"])
}
func decodeImage(url string) (string, []byte, error) {
	header, payload, ok := strings.Cut(url, ",")
	if !ok || !strings.HasSuffix(header, ";base64") {
		return "", nil, fail(400, "image must use a base64 data URL")
	}
	mime := strings.TrimSuffix(strings.TrimPrefix(header, "data:"), ";base64")
	formats := map[string]string{"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}
	want := formats[mime]
	if want == "" || len(payload) > 4*((20<<20)+2)/3+4 {
		return "", nil, fail(400, "unsupported image type or image exceeds 20 MiB")
	}
	data, err := base64.StdEncoding.Strict().DecodeString(payload)
	if err != nil || len(data) == 0 || len(data) > 20<<20 {
		return "", nil, fail(400, "invalid or oversized image data")
	}
	cfg, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil || format != want || cfg.Width < 1 || cfg.Height < 1 || int64(cfg.Width)*int64(cfg.Height) > 64<<20 {
		return "", nil, fail(400, "invalid image dimensions or mismatched image format")
	}
	return mime, data, nil
}
func validateImages(body object) error {
	total := 0
	for _, part := range imageParts(body) {
		url := imageURL(part)
		if strings.HasPrefix(url, "data:") {
			_, data, err := decodeImage(url)
			if err != nil {
				return err
			}
			total += len(data)
			if total > 32<<20 {
				return fail(400, "request images exceed 32 MiB")
			}
		} else if str(part["file_id"]) == "" {
			return fail(400, "only inline data images or existing BPS file_id are supported")
		}
	}
	return nil
}
func hasInlineImages(body object) bool {
	for _, part := range imageParts(body) {
		if strings.HasPrefix(imageURL(part), "data:") {
			return true
		}
	}
	return false
}
func (s *upstreamStream) uploadImages(body object, sess session) error {
	// Deduplicate within this request only; never reuse attachments across accounts.
	uploaded := map[string]string{}
	for _, part := range imageParts(body) {
		url := imageURL(part)
		if !strings.HasPrefix(url, "data:") {
			continue
		}
		mime, data, err := decodeImage(url)
		if err != nil {
			return err
		}
		digest := hash(url)
		id := uploaded[digest]
		if id == "" {
			var payload bytes.Buffer
			writer := multipart.NewWriter(&payload)
			h := textproto.MIMEHeader{}
			h.Set("Content-Disposition", `form-data; name="file"; filename="picture-`+digest[:12]+`.`+strings.TrimPrefix(mime, "image/")+`"`)
			h.Set("Content-Type", mime)
			partWriter, err := writer.CreatePart(h)
			if err != nil {
				return err
			}
			if _, err = partWriter.Write(data); err != nil {
				return err
			}
			if err = writer.Close(); err != nil {
				return err
			}
			headers := sess.headers()
			headers.Set("Content-Type", writer.FormDataContentType())
			headers.Set("Accept", "application/json")
			s.p.throttle()
			s.mu.Lock()
			op, closed := s.operation, s.closed
			s.mu.Unlock()
			if closed {
				return fail(504, "image upload operation closed")
			}
			var res struct {
				StatusCode int
				Body       []byte
			}
			err = s.p.host("host.http.do", object{"host_callback_id": s.callback, "operation_id": op, "method": "POST",
				"url": "https://bps.openai.com/basispoints/api/attachments", "headers": headers, "body": payload.Bytes()}, &res)
			if err != nil {
				return err
			}
			if res.StatusCode < 200 || res.StatusCode >= 300 {
				status := 502
				if res.StatusCode == 401 || res.StatusCode == 403 || res.StatusCode == 429 {
					status = res.StatusCode
				}
				return fail(status, "BPS attachment upload failed")
			}
			var data object
			if len(res.Body) > 64<<10 || json.Unmarshal(res.Body, &data) != nil {
				return fail(502, "invalid attachment response")
			}
			id = str(data["openai_file_id"])
			if id == "" {
				return fail(502, "attachment response has no file ID")
			}
			uploaded[digest] = id
		}
		delete(part, "image_url")
		part["file_id"] = id
		if _, ok := part["detail"]; !ok {
			part["detail"] = "auto"
		}
	}
	return nil
}
