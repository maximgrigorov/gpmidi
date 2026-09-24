// Offline MIDI -> WAV for listening checks (macOS only, no downloads).
// AVAudioEngine manual rendering + AVAudioUnitSampler with the system GS DLS.
// Pitch-bend RPN is honoured (range 7 verified: A4 + 8191 -> 661 Hz).
// Build: swiftc -O tools/render_midi_offline.swift -o <bin>
// Usage: <bin> in.mid out.wav <gm_program 0-127> [tail_seconds]
import AVFoundation
import AudioToolbox

// usage: render <in.mid> <out.wav> <gm_program> [tail_seconds]
let args = CommandLine.arguments
let midiURL = URL(fileURLWithPath: args[1])
let outURL = URL(fileURLWithPath: args[2])
let program = UInt8(Int(args[3])!)
let tail = args.count > 4 ? Double(args[4])! : 2.0

let engine = AVAudioEngine()
let sampler = AVAudioUnitSampler()
engine.attach(sampler)
engine.connect(sampler, to: engine.mainMixerNode, format: nil)
let dls = URL(fileURLWithPath: "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls")
try sampler.loadSoundBankInstrument(at: dls, program: program,
    bankMSB: UInt8(kAUSampler_DefaultMelodicBankMSB), bankLSB: UInt8(kAUSampler_DefaultBankLSB))

let sr = 44100.0
let format = AVAudioFormat(standardFormatWithSampleRate: sr, channels: 2)!
try engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 1024)
let sequencer = AVAudioSequencer(audioEngine: engine)
try sequencer.load(from: midiURL, options: [])
for t in sequencer.tracks { t.destinationAudioUnit = sampler }
try engine.start()
sequencer.prepareToPlay()
try sequencer.start()

var lengthSec = 0.0
for t in sequencer.tracks { lengthSec = max(lengthSec, sequencer.seconds(forBeats: t.lengthInBeats)) }
let total = AVAudioFramePosition((lengthSec + tail) * sr)
let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: sr,
    AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false,
    AVLinearPCMIsBigEndianKey: false]
func renderAll() throws {
    // AVAudioFile finalises the WAV header only on deinit: keep it function-local.
    let file = try AVAudioFile(forWriting: outURL, settings: settings,
                               commonFormat: .pcmFormatFloat32, interleaved: false)
    let buffer = AVAudioPCMBuffer(pcmFormat: engine.manualRenderingFormat,
                                  frameCapacity: engine.manualRenderingMaximumFrameCount)!
    while engine.manualRenderingSampleTime < total {
        let frames = min(buffer.frameCapacity, AVAudioFrameCount(total - engine.manualRenderingSampleTime))
        let status = try engine.renderOffline(frames, to: buffer)
        if status == .success { try file.write(from: buffer) } else { print("status", status.rawValue); break }
    }
}
try renderAll()
print("rendered", String(format: "%.1f", lengthSec), "s ->", outURL.path)
