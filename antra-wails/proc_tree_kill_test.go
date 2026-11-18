package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// These tests pin the contract that Stop depends on: killProcessTree must kill
// the processes the backend spawned, not just the PID we happen to hold.
//
// That distinction is the whole of issue #82. The shipped backend is a
// PyInstaller *onefile* binary, whose bootloader forks a second process that
// runs the real Python engine. On macOS and Linux the old code called
// cmd.Process.Kill(), which reaped the bootloader and left the engine
// downloading. TestKillOnlyParentLeavesGrandchildAlive is the negative control
// for that: it performs the OLD kill and asserts the grandchild survives, so a
// pass on TestKillProcessTreeKillsGrandchild cannot be vacuous.
//
// The grandchild is spawned by the parent without any process-group setup of
// its own, mirroring the bootloader's fork.

const (
	roleEnv  = "ANTRA_TEST_TREE_ROLE"
	fileEnv  = "ANTRA_TEST_TREE_FILE"
	pidEnv   = "ANTRA_TEST_TREE_PIDFILE"
	beatEvry = 50 * time.Millisecond
)

// helperProcessDispatch re-enters the test binary as the fake process tree.
// It is called from TestMain (spectrum_test.go) before the suite runs.
func helperProcessDispatch() {
	switch os.Getenv(roleEnv) {
	case "parent":
		child := exec.Command(os.Args[0])
		child.Env = append(os.Environ(), roleEnv+"=grandchild")
		// Deliberately no setProcessGroup here: the grandchild must inherit
		// the parent's group, exactly as PyInstaller's forked engine does.
		if err := child.Start(); err != nil {
			os.Exit(1)
		}
		time.Sleep(60 * time.Second)
		os.Exit(0)
	case "grandchild":
		if pf := os.Getenv(pidEnv); pf != "" {
			_ = os.WriteFile(pf, []byte(strconv.Itoa(os.Getpid())), 0o644)
		}
		path := os.Getenv(fileEnv)
		deadline := time.Now().Add(60 * time.Second)
		for time.Now().Before(deadline) {
			f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
			if err == nil {
				_, _ = f.Write([]byte("x"))
				_ = f.Close()
			}
			time.Sleep(beatEvry)
		}
		os.Exit(0)
	}
}

type fakeTree struct {
	cmd      *exec.Cmd
	beatFile string
	pidFile  string
}

// startFakeTree launches parent -> grandchild and returns once the grandchild
// is demonstrably alive, so a later "it stopped" reading means something.
func startFakeTree(t *testing.T) *fakeTree {
	t.Helper()

	dir := t.TempDir()
	tree := &fakeTree{
		beatFile: filepath.Join(dir, "heartbeat"),
		pidFile:  filepath.Join(dir, "grandchild.pid"),
	}

	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(),
		roleEnv+"=parent",
		fileEnv+"="+tree.beatFile,
		pidEnv+"="+tree.pidFile,
	)
	hideProcess(cmd)
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start fake tree: %v", err)
	}
	go func() { _ = cmd.Wait() }()
	tree.cmd = cmd

	t.Cleanup(func() {
		_ = killProcessTree(cmd)
		tree.killGrandchild()
	})

	// Control: the probe must be doing work before we trust a null reading.
	if !tree.growsWithin(3 * time.Second) {
		t.Fatalf("grandchild never wrote a heartbeat - the harness is not measuring anything")
	}
	return tree
}

// growsWithin reports whether the heartbeat file gets bigger inside d.
func (f *fakeTree) growsWithin(d time.Duration) bool {
	start := f.size()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if f.size() > start {
			return true
		}
		time.Sleep(beatEvry)
	}
	return false
}

func (f *fakeTree) size() int64 {
	st, err := os.Stat(f.beatFile)
	if err != nil {
		return 0
	}
	return st.Size()
}

func (f *fakeTree) killGrandchild() {
	raw, err := os.ReadFile(f.pidFile)
	if err != nil {
		return
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 {
		return
	}
	if p, err := os.FindProcess(pid); err == nil {
		_ = p.Kill()
	}
}

func TestKillProcessTreeKillsGrandchild(t *testing.T) {
	tree := startFakeTree(t)

	if err := killProcessTree(tree.cmd); err != nil {
		t.Fatalf("killProcessTree: %v", err)
	}

	// Let anything already in flight land, then measure.
	time.Sleep(300 * time.Millisecond)
	if tree.growsWithin(2 * time.Second) {
		t.Fatalf("grandchild kept running after killProcessTree - Stop does not stop the download")
	}
}

// TestKillOnlyParentLeavesGrandchildAlive reproduces the reported bug with the
// pre-fix kill, proving the test above can actually fail.
func TestKillOnlyParentLeavesGrandchildAlive(t *testing.T) {
	tree := startFakeTree(t)

	if err := tree.cmd.Process.Kill(); err != nil {
		t.Fatalf("kill parent: %v", err)
	}

	time.Sleep(300 * time.Millisecond)
	if !tree.growsWithin(2 * time.Second) {
		t.Fatalf("negative control did not reproduce: killing only the parent " +
			"stopped the grandchild too, so this harness cannot detect the bug")
	}

	tree.killGrandchild()
}
