//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

// hideProcess is a no-op on non-Windows platforms.
// On macOS and Linux, child processes don't create visible console windows.
func hideProcess(cmd *exec.Cmd) {}

// setProcessGroup puts the child in a process group of its own so the whole
// tree can be signalled at once.
//
// This is load-bearing for Stop, and it is why Stop looked broken on macOS
// and Linux while working on Windows. The bundled backend is a PyInstaller
// *onefile* binary: its bootloader forks a second process that runs the real
// Python engine, and the engine in turn spawns ffmpeg. cmd.Process.Pid is the
// bootloader, so killing it alone orphans the engine, which keeps downloading
// with nothing left listening to its stdout. Windows never had the bug because
// it kills the tree with `taskkill /T`.
func setProcessGroup(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// killProcessTree terminates cmd and every process it spawned.
func killProcessTree(cmd *exec.Cmd) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}
	pid := cmd.Process.Pid

	// A negative pid addresses the whole process group. Only do that when the
	// child actually leads its own group (setProcessGroup ran at spawn time) —
	// otherwise the group is ours and we would kill the app itself.
	if pgid, err := syscall.Getpgid(pid); err == nil && pgid == pid {
		if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			return err
		}
		return nil
	}

	if err := cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
		return err
	}
	return nil
}
