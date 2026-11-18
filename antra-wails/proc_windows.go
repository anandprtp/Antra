//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"strings"
	"syscall"
)

// hideProcess configures a command to run hidden on Windows (no console window).
func hideProcess(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
}

// setProcessGroup is a no-op on Windows: killProcessTree already walks the
// tree with `taskkill /T`, which needs no grouping set up in advance.
func setProcessGroup(cmd *exec.Cmd) {}

// killProcessTree terminates cmd and every process it spawned.
func killProcessTree(cmd *exec.Cmd) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}

	killer := exec.Command("taskkill", "/PID", fmt.Sprintf("%d", cmd.Process.Pid), "/T", "/F")
	hideProcess(killer)
	output, err := killer.CombinedOutput()
	if err != nil {
		text := strings.ToLower(string(output))
		if strings.Contains(text, "not found") || strings.Contains(text, "no running instance") {
			return nil
		}
		return fmt.Errorf("taskkill failed: %v (%s)", err, strings.TrimSpace(string(output)))
	}
	return nil
}
