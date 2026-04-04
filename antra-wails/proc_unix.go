//go:build !windows

package main

import "os/exec"

// hideProcess is a no-op on non-Windows platforms.
// On macOS and Linux, child processes don't create visible console windows.
func hideProcess(cmd *exec.Cmd) {}
