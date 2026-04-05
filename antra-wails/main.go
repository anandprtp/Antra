package main

import (
	"embed"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	"github.com/wailsapp/wails/v2/pkg/options/windows"
)

//go:embed all:frontend/dist
var assets embed.FS

//go:embed build/appicon.png
var appIcon []byte

func main() {
	// Create an instance of the app structure
	app := NewApp()

	// Create application with options
	err := wails.Run(&options.App{
		Title:     "Antra",
		Width:     1024,
		Height:    768,
		MinWidth:  720,
		MinHeight: 480,
		AssetServer: &assetserver.Options{
			Assets: assets,
		},
		// A: 255 (fully opaque) — Wails uses 0-255 scale, not 0.0-1.0
		BackgroundColour: &options.RGBA{R: 13, G: 28, B: 28, A: 255},
		// Start hidden to avoid the white/unstyled flash while the frontend loads.
		// The window is revealed in OnDomReady once the Svelte app has mounted.
		StartHidden:  true,
		OnStartup:    app.startup,
		OnDomReady:   app.domReady,
		OnShutdown:   app.shutdown,
		Icon:         appIcon,
		Windows: &windows.Options{
			WebviewIsTransparent: false,
			WindowIsTranslucent:  false,
			DisableWindowIcon:    false,
		},
		Bind: []interface{}{
			app,
		},
	})

	if err != nil {
		println("Error:", err.Error())
	}
}
