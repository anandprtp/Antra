//go:build !windows

package main

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
)

//go:embed all:runtime
var runtimeAssets embed.FS

func ensureBundledBackend() (string, error) {
	const backendAsset = "runtime/backend/AntraBackend"

	payload, err := runtimeAssets.ReadFile(backendAsset)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return "", fs.ErrNotExist
		}
		return "", err
	}
	if len(payload) == 0 {
		return "", fs.ErrNotExist
	}

	sum := sha256.Sum256(payload)
	versionDir := filepath.Join(getAppDataDir(), "runtime", "backend", hex.EncodeToString(sum[:8]))
	backendPath := filepath.Join(versionDir, "AntraBackend")

	if data, err := os.ReadFile(backendPath); err == nil {
		if sha256.Sum256(data) == sum {
			return backendPath, nil
		}
	}

	if err := os.MkdirAll(versionDir, 0755); err != nil {
		return "", err
	}

	tmpPath := backendPath + ".tmp"
	if err := os.WriteFile(tmpPath, payload, 0755); err != nil {
		return "", err
	}
	if err := os.Rename(tmpPath, backendPath); err != nil {
		_ = os.Remove(tmpPath)
		return "", err
	}
	// Ensure the binary is executable
	if err := os.Chmod(backendPath, 0755); err != nil {
		return "", err
	}

	return backendPath, nil
}
