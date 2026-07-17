#!/bin/zsh
set -u

PROJECT_DIR=${0:A:h}

selection_mode=$(osascript \
  -e 'button returned of (display dialog "Choose individual email archive files or a folder containing them." buttons {"Choose Folder", "Choose Files"} default button "Choose Files")') || exit 0

if [[ "$selection_mode" == "Choose Folder" ]]; then
  folder_path=$(osascript -e 'POSIX path of (choose folder with prompt "Choose a folder containing PST, MBOX, or EML files")') || exit 0
  source_paths=("${folder_path%/}")
else
  source_output=$(osascript \
    -e 'set selectedFiles to choose file with prompt "Choose PST, MBOX, or EML files" with multiple selections allowed' \
    -e 'set selectedPaths to ""' \
    -e 'repeat with selectedFile in selectedFiles' \
    -e 'set selectedPaths to selectedPaths & POSIX path of selectedFile & linefeed' \
    -e 'end repeat' \
    -e 'return selectedPaths') || exit 0
  source_paths=("${(@f)source_output}")
fi
destination_parent=$(osascript -e 'POSIX path of (choose folder with prompt "Choose where the exported mail should be saved")') || exit 0

if (( ${#source_paths[@]} == 1 )); then
  source_name=${source_paths[1]:t:r}
else
  source_name="combined-email-corpus"
fi
destination="${destination_parent%/}/${source_name}-ai-export"
if [[ -e "$destination" ]]; then
  destination="${destination}-$(date +%Y%m%d-%H%M%S)"
fi

echo "Exporting mail locally..."
echo "Sources:"
printf '  %s\n' "${source_paths[@]}"
echo "Destination: $destination"
echo

PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m pst_ai_exporter "${source_paths[@]}" --output "$destination"
status=$?

if [[ $status -eq 0 ]]; then
  echo
  echo "Export finished successfully."
  open "$destination"
else
  echo
  echo "The export did not finish. Review the error above."
fi

echo
read "?Press Return to close this window."
exit $status
