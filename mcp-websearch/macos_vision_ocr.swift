import AppKit
import Foundation
import Vision

func recognizedLines(in path: String) throws -> [String] {
    let url = URL(fileURLWithPath: path)
    guard
        let image = NSImage(contentsOf: url),
        let data = image.tiffRepresentation,
        let bitmap = NSBitmapImageRep(data: data),
        let cgImage = bitmap.cgImage
    else {
        throw NSError(domain: "local-search-ocr", code: 2, userInfo: [
            NSLocalizedDescriptionKey: "Could not decode image at \(path)"
        ])
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])

    return (request.results ?? [])
        .sorted { left, right in
            let leftY = left.boundingBox.midY
            let rightY = right.boundingBox.midY
            if abs(leftY - rightY) > 0.01 {
                return leftY > rightY
            }
            return left.boundingBox.minX < right.boundingBox.minX
        }
        .compactMap { $0.topCandidates(1).first?.string }
}

let imagePaths = Array(CommandLine.arguments.dropFirst())
if imagePaths.isEmpty {
    fputs("Usage: swift macos_vision_ocr.swift <page-image> [...]\n", stderr)
    exit(2)
}

do {
    for (index, path) in imagePaths.enumerated() {
        print("=== PAGE \(index + 1) ===")
        for line in try recognizedLines(in: path) {
            print(line)
        }
        print("")
    }
} catch {
    fputs("OCR failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
