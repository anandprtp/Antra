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

// prepareBackendProcess puts the backend in its own process group so
// killCommandTree can signal the whole tree (ffmpeg/python children included).
func prepareBackendProcess(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// killUnixProcessTree SIGKILLs the process group started with Setpgid.
func killUnixProcessTree(cmd *exec.Cmd) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}
	pgid := cmd.Process.Pid
	if err := syscall.Kill(-pgid, syscall.SIGKILL); err != nil {
		if err := cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
			return err
		}
	}
	return nil
}
