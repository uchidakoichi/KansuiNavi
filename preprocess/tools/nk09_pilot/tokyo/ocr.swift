// macOS Vision の OCR。行ごとに「x y w h<TAB>文字列」を出す（座標は画像の左上原点・0〜1）
import Vision
import AppKit
let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ja-JP", "en-US"]
req.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cg).perform([req])
for o in req.results ?? [] {
  guard let t = o.topCandidates(1).first else { continue }
  let b = o.boundingBox
  print(String(format: "%.4f %.4f %.4f %.4f\t", b.minX, 1 - b.maxY, b.width, b.height) + t.string)
}
