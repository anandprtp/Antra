package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const defaultSupportStatusURL = "https://gist.githubusercontent.com/anandprtp/8c74e99cb3fb8dc5347b5c6aa445d057/raw"

type SupportStatus struct {
	Enabled  bool    `json:"enabled"`
	Title    string  `json:"title"`
	Message  string  `json:"message"`
	Current  float64 `json:"current"`
	Goal     float64 `json:"goal"`
	Currency string  `json:"currency"`
	Link     string  `json:"link"`
}

type supportStatusPatch struct {
	Enabled  *bool    `json:"enabled"`
	Title    string   `json:"title"`
	Message  string   `json:"message"`
	Current  *float64 `json:"current"`
	Goal     *float64 `json:"goal"`
	Currency string   `json:"currency"`
	Link     string   `json:"link"`
}

func (a *App) GetSupportStatus() string {
	status := defaultSupportStatus()

	if local, err := readSupportStatusFile(getSupportStatusPath()); err == nil && local != nil {
		status = mergeSupportStatus(status, *local)
	}

	remoteURL := strings.TrimSpace(os.Getenv("ANTRA_SUPPORT_STATUS_URL"))
	if remoteURL == "" {
		remoteURL = defaultSupportStatusURL
	}
	if remoteURL != "" {
		if remote, err := fetchSupportStatus(remoteURL); err == nil && remote != nil {
			status = mergeSupportStatus(status, *remote)
			_ = writeSupportStatusFile(getSupportStatusCachePath(), status)
		} else if cached, err := readSupportStatusFile(getSupportStatusCachePath()); err == nil && cached != nil {
			status = mergeSupportStatus(status, *cached)
		}
	}

	data, err := json.Marshal(status)
	if err != nil {
		return `{"enabled":false}`
	}
	return string(data)
}

func defaultSupportStatus() SupportStatus {
	return SupportStatus{
		Enabled:  true,
		Title:    "Support Antra",
		Message:  "Solo-maintained by one developer. Help fund bug fixes, updates, and endpoint costs.",
		Current:  0,
		Goal:     200,
		Currency: "USD",
		Link:     "https://ko-fi.com/antraverse",
	}
}

func getSupportStatusPath() string {
	return filepath.Join(getAppDataDir(), "support_status.json")
}

func getSupportStatusCachePath() string {
	return filepath.Join(getAppDataDir(), "support_status_cache.json")
}

func readSupportStatusFile(path string) (*supportStatusPatch, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var status supportStatusPatch
	if err := json.Unmarshal(data, &status); err != nil {
		return nil, err
	}
	return &status, nil
}

func writeSupportStatusFile(path string, status SupportStatus) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0644)
}

func fetchSupportStatus(remoteURL string) (*supportStatusPatch, error) {
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(remoteURL)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, os.ErrNotExist
	}
	var status supportStatusPatch
	if err := json.NewDecoder(resp.Body).Decode(&status); err != nil {
		return nil, err
	}
	return &status, nil
}

func mergeSupportStatus(base SupportStatus, override supportStatusPatch) SupportStatus {
	merged := base
	if override.Enabled != nil {
		merged.Enabled = *override.Enabled
	}
	if override.Title != "" {
		merged.Title = override.Title
	}
	if override.Message != "" {
		merged.Message = override.Message
	}
	if override.Current != nil && *override.Current >= 0 {
		merged.Current = *override.Current
	}
	if override.Goal != nil && *override.Goal > 0 {
		merged.Goal = *override.Goal
	}
	if override.Currency != "" {
		merged.Currency = override.Currency
	}
	if override.Link != "" {
		merged.Link = override.Link
	}
	return merged
}
