"""Command-line backend for the PRC desktop UI."""
import argparse
import json
import multiprocessing
import os
from pathlib import Path

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}


def count_images(folder):
    root = Path(folder)
    return sum(1 for p in root.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and 'prc_debug_output' not in str(p).lower())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['all', 'extract', 'ocr'], required=True)
    parser.add_argument('--input-folder')
    parser.add_argument('--image-folder', required=True)
    parser.add_argument('--scheme-master')
    parser.add_argument('--fund-column', default='Fund Name')
    parser.add_argument('--output-excel')
    parser.add_argument('--zoom', type=float, default=3.0)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--threshold', type=float, default=88.0)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    image_folder = Path(args.image_folder).resolve()
    image_folder.mkdir(parents=True, exist_ok=True)
    summary = {'mode': args.mode, 'image_folder': str(image_folder)}

    if args.mode in ('all', 'extract'):
        if not args.input_folder or not Path(args.input_folder).is_dir():
            raise FileNotFoundError('A valid factsheet folder is required.')
        import extract_prc
        print('STAGE|Extracting images', flush=True)
        before = count_images(image_folder)
        extract_prc.process_folder(str(Path(args.input_folder).resolve()), str(image_folder), args.zoom)
        after = count_images(image_folder)
        summary.update({'images_before': before, 'images_after': after, 'images_created': max(0, after-before)})
        if after == 0:
            raise RuntimeError('Extraction finished but no PRC images were created. Review AMC detection messages above.')
        print(f'STAGE|Extraction complete - {after} images available', flush=True)

    if args.mode in ('all', 'ocr'):
        if not args.scheme_master or not Path(args.scheme_master).is_file():
            raise FileNotFoundError('A valid Scheme Master.xlsx file is required for fund-name matching.')
        if not args.output_excel:
            raise ValueError('An Excel save path is required.')
        output = Path(args.output_excel).resolve()
        if output.suffix.lower() != '.xlsx':
            output = output.with_suffix('.xlsx')
        output.parent.mkdir(parents=True, exist_ok=True)
        print('STAGE|Detecting PRC and matching Scheme Master', flush=True)
        import Main_PRC
        result = Main_PRC.process_prc_folder_with_easyocr(
            root_folder=str(image_folder),
            scheme_master_path=str(Path(args.scheme_master).resolve()),
            scheme_master_fund_col=args.fund_column,
            output_excel=str(output),
            debug=args.debug,
            parallel_workers=args.workers,
            fuzzy_threshold=args.threshold,
        )
        summary.update(result)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError('The requested Excel output was not created.')
        print(f'STAGE|Excel saved - {output}', flush=True)

    print('RESULT|' + json.dumps(summary), flush=True)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
