//go:build windows

package main

import (
	"os/exec"
	"syscall"
)

// hideProcess configures a command to run hidden on Windows (no console window).
func hideProcess(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
}

// prepareBackendProcess is a no-op on Windows; taskkill /T covers the process tree.
func prepareBackendProcess(cmd *exec.Cmd) {}

// killUnixProcessTree is unused on Windows (killCommandTree uses taskkill /T).
func killUnixProcessTree(cmd *exec.Cmd) error { return nil }
