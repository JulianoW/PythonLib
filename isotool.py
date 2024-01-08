import struct
import argparse
from pathlib import Path
from dataclasses import dataclass

SCRIPT_VERSION = "1.0"


@dataclass
class FileListData:
    path: Path
    inode: int
    lba: int


@dataclass
class FileListInfo:
    files: list[FileListData]
    total_inodes: int


def main():
    print(f"pyPS2 ISO Rebuilder v{SCRIPT_VERSION}")
    print("Original by Raynê Games")

    args = get_arguments()

    if args.mode == "extract":
        dump_iso(args.iso, args.filelist, args.files)
        print("dumping finished")
    else:
        rebuild_iso(args.iso, args.filelist, args.files, args.output, args.with_padding)
        print("rebuild finished")


def get_arguments(argv=None):
    # Init argument parser
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-m",
        "--mode",
        choices=["extract", "insert"],
        required=True,
        metavar="operation",
        help="Options: extract, insert",
    )

    parser.add_argument(
        "--iso",
        required=True,
        type=Path,
        metavar="original_iso",
        help="input game iso file path",
    )

    parser.add_argument(
        "--with-padding",
        required=False,
        action="store_true",
        help="flag to control outermost iso padding",
    )

    parser.add_argument(
        "-o",
        "--output",
        required=False,
        type=Path,
        metavar="output_iso",
        help="resulting iso file name",
    )

    parser.add_argument(
        "--filelist",
        required=False,
        type=Path,
        metavar="filelist_path",
        help="filelist.txt file path",
    )

    parser.add_argument(
        "--files",
        required=False,
        type=Path,
        metavar="files_folder",
        help="path to folder with extracted iso files",
    )

    args = parser.parse_args()
    curr_dir = Path("./").resolve()

    args.iso = args.iso.resolve()
    if hasattr(args, "filelist") and not args.filelist:
        args.filelist = curr_dir / f"{args.iso.name.upper()}-FILELIST-LSN.TXT"

    if hasattr(args, "files") and not args.files:
        args.files = curr_dir / f"@{args.iso.name.upper()}"

    if hasattr(args, "output") and not args.output:
        args.output = curr_dir / f"NEW_{args.iso.name}"

    return args


def dump_iso(iso_path: Path, filelist: Path, iso_files: Path) -> None:
    iso_files.mkdir(parents=True, exist_ok=True)

    with open(iso_path, "rb") as iso:
        # Traverse directory records recursively
        iso.seek(0x809E)
        path_parts = []
        record_ends = []
        record_pos = []
        file_info = FileListInfo([], 0)

        dr_data_pos, dr_data_len = struct.unpack("<I4xI", iso.read(12))
        dr_data_pos *= 0x800
        record_ends.append(dr_data_pos + dr_data_len)
        record_pos.append(0)
        iso.seek(dr_data_pos)

        while True:
            if iso.tell() >= record_ends[-1]:
                if len(record_ends) == 1:
                    break
                else:
                    record_ends.pop()
                    path_parts.pop()
                    iso.seek(record_pos.pop())

            inode = iso.tell()
            dr_len = struct.unpack("<B", iso.read(1))[0]
            dr_blob = iso.read(dr_len - 1)

            (
                dr_ea_len,
                dr_data_pos,
                dr_data_len,
                dr_flags,
                dr_inter,
                dr_volume,
                dr_name_len,
            ) = struct.unpack_from("<BI4xI4x7xBHH2xB", dr_blob)

            assert dr_ea_len == 0, "ISOs with extra attributes are not supported!"
            assert dr_inter == 0, "Interleaved ISOs are not supported!"
            assert dr_volume == 1, "multi-volume ISOs are not supported!"
            assert (dr_flags & 0b1000000) == 0, "4GiB+ files are not supported!"

            if (iso.tell() % 2) != 0:
                iso.read(1)

            dr_data_pos *= 0x800

            dr_name = dr_blob[32 : 32 + dr_name_len]

            if dr_name == b"\x00" or dr_name == b"\x01":
                continue

            dr_name = dr_name.decode()
            if dr_name.endswith(";1"):
                dr_name = dr_name[:-2]
            path_parts.append(dr_name)

            # is it a directory?
            file_info.total_inodes += 1
            if (dr_flags & 0b10) != 0:
                record_pos.append(iso.tell())
                record_ends.append(dr_data_pos + dr_data_len)
                fp = iso_files / "/".join(path_parts)
                fp.mkdir(exist_ok=True)
                iso.seek(dr_data_pos)
                continue
            else:
                fp = "/".join(path_parts)
                print(f"saving {fp}")

                save_pos = iso.tell()
                with open(iso_files / fp, "wb+") as f:
                    iso.seek(dr_data_pos)
                    f.write(iso.read(dr_data_len))
                iso.seek(save_pos)

                file_info.files.append(FileListData(Path(fp), inode, dr_data_pos))
                path_parts.pop()

        file_info.files = sorted(file_info.files, key=lambda x: x.lba)

        with open(filelist, "w", encoding="utf8") as f:
            for d in file_info.files:
                f.write(f"|{d.inode}||{iso_files.name}/{d.path}|\n")
            f.write(f"//{file_info.total_inodes}")


def rebuild_iso(
    iso: Path, filelist: Path, iso_files: Path, output: Path, add_padding: bool
) -> None:
    if filelist.exists() is False:
        print(f"Could not to find the '{filelist.name}' files log!")
        return

    if iso_files.exists() is False:
        print(f"Could not to find the '{iso_files.name}' files directory!")
        return

    if iso_files.is_dir() is False:
        print(f"'{iso_files.name}' is not a directory!")
        return

    with open(filelist, "r") as f:
        lines = f.readlines()

    inode_data: list[FileListData] = []
    for line in lines[:-1]:
        l = [x for x in line.split("|") if x]
        p = Path(l[1])
        inode_data.append(FileListData(Path(*p.parts[1:]), int(l[0]), 0))

    if lines[-1].startswith("//") is False:
        print(f"Could not to find the '{filelist.name}' inode total!")
        return

    iso_info = FileListInfo(inode_data, int(lines[-1][2:]))

    with open(iso, "rb") as f:
        header = f.read(0xF60000)
        i = 0
        data_start = -1
        for lba in range(7862):
            udf_check = struct.unpack_from("<269x18s1761x", header, lba * 0x800)[0]
            if udf_check == b"*UDF DVD CGMS Info":
                i += 1

            if i == iso_info.total_inodes + 1:
                data_start = (lba + 1) * 0x800
                break
        else:
            print(
                "ERROR: Couldn't get all the UDF file chunk, original tool would've looped here"
            )
            print("Closing instead...")
            return

        f.seek(-0x800, 2)
        footer = f.read(0x800)

    with open(output, "wb+") as f:
        f.write(header[:data_start])

        for inode in inode_data:
            fp = iso_files / inode.path
            start_pos = f.tell()
            if fp.exists() is False:
                print(f"File '{inode.path}' not found!")
                return

            print(f"Inserting {str(inode.path)}...")

            with open(fp, "rb") as g:
                while data := g.read(0x80000):
                    f.write(data)

            end_pos = f.tell()

            # Align to next LBA
            al_end = (end_pos + 0x7FF) & ~(0x7FF)
            f.write(b"\x00" * (al_end - end_pos))

            end_save = f.tell()

            new_lba = start_pos // 0x800
            new_size = end_pos - start_pos
            f.seek(inode.inode + 2)

            f.write(struct.pack("<I", new_lba))
            f.write(struct.pack(">I", new_lba))
            f.write(struct.pack("<I", new_size))
            f.write(struct.pack(">I", new_size))

            f.seek(end_save)

        # Align to 0x8000
        end_pos = f.tell()
        al_end = (end_pos + 0x7FFF) & ~(0x7FFF)
        f.write(b"\x00" * (al_end - end_pos))

        # Sony's cdvdgen tool starting with v2.00 by deafault adds
        # a 20MiB padding to the end of the PVD, add it here if requested
        # minus a whole LBA for the end of file Anchor
        if add_padding:
            f.write(b"\x00" * (0x140_0000 - 0x800))

        last_pvd_lba = f.tell() // 0x800

        f.write(footer)
        f.seek(0x8050)
        f.write(struct.pack("<I", last_pvd_lba))
        f.write(struct.pack(">I", last_pvd_lba))
        f.seek(-0x7F4, 2)
        f.write(struct.pack("<I", last_pvd_lba))


if __name__ == "__main__":
    main()
