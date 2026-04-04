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
