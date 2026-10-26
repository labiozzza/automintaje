"""Browser regression probe for the edited silence-preview toggle.

Run the local app first, then execute this file with the project's Python and
Playwright environment. The probe uses synthetic cuts in browser memory only.
"""

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8000/"


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(BASE_URL, wait_until="domcontentloaded")
        result = page.evaluate(
            """() => {
              const video = document.getElementById('projectPreviewVideo');
              const trim = document.getElementById('silenceTrimEnabled');
              const editedButton = document.getElementById('silenceModeEdited');
              const sourceButton = document.getElementById('silenceModeSource');
              const status = document.getElementById('silenceStatus');
              const saved = {
                trim: trim.checked,
                cuts: silenceCuts,
                analysis: silenceAnalysis,
                selected: silenceSelectedCutId,
                mode: silencePlaybackMode,
                force: silencePreviewForceEdited,
                play: Object.getOwnPropertyDescriptor(video, 'play'),
                currentTime: Object.getOwnPropertyDescriptor(video, 'currentTime'),
                duration: Object.getOwnPropertyDescriptor(video, 'duration'),
                statusText: status.textContent,
                statusState: status.dataset.state,
              };
              let playCalls = 0;
              let result;
              try {
                trim.checked = false;
                silenceAnalysis = {source_duration_ms: 10000, fps: '30/1', audio_available: true};
                silenceCuts = [{cut_id: 'probe-cut', start_ms: 1000, end_ms: 2000, origin: 'manual'}];
                silenceSelectedCutId = 'probe-cut';
                silencePlaybackMode = 'source';
                silencePreviewForceEdited = false;
                Object.defineProperty(video, 'duration', {configurable: true, value: 10});
                Object.defineProperty(video, 'currentTime', {configurable: true, writable: true, value: 1.5});
                video.play = () => { playCalls += 1; return Promise.resolve(); };

                editedButton.click();
                const selected = editedButton.getAttribute('aria-pressed') === 'true';
                const mode = silencePlaybackMode;
                const previewTime = silenceOutputTime(3000);
                const playCallsAfterClick = playCalls;

                video.currentTime = 1.5;
                video.dispatchEvent(new Event('timeupdate'));
                result = {
                  renderTrimmingRemainsOff: !trim.checked,
                  selected,
                  mode,
                  playCalls: playCallsAfterClick,
                  previewTime,
                  skippedTo: video.currentTime,
                };
              } finally {
                trim.checked = saved.trim;
                silenceCuts = saved.cuts;
                silenceAnalysis = saved.analysis;
                silenceSelectedCutId = saved.selected;
                silencePlaybackMode = saved.mode;
                silencePreviewForceEdited = saved.force;
                if (saved.play) Object.defineProperty(video, 'play', saved.play);
                else delete video.play;
                if (saved.currentTime) Object.defineProperty(video, 'currentTime', saved.currentTime);
                else delete video.currentTime;
                if (saved.duration) Object.defineProperty(video, 'duration', saved.duration);
                else delete video.duration;
                editedButton.setAttribute('aria-pressed', String(saved.mode === 'edited'));
                sourceButton.setAttribute('aria-pressed', String(saved.mode === 'source'));
                silenceSetStatus(saved.statusText, saved.statusState);
              }
              return result;
            }"""
        )

        assert result["renderTrimmingRemainsOff"] is True, result
        assert result["selected"] is True, result
        assert result["mode"] == "edited", result
        assert result["playCalls"] == 1, result
        assert result["previewTime"] < 3000, result
        assert abs(result["skippedTo"] - 2.0) < 0.01, result
        assert not page_errors, page_errors
        print("silence preview regression: PASS", result)
        browser.close()


if __name__ == "__main__":
    main()
