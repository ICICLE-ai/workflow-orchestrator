python generate_geopackage.py \
    --json /fs/ess/PAS2699/Demo_data/outputs/out1/annotations_dinov3_sam3_20260724_104322.json \
    --outdir /fs/ess/PAS2699/Demo_data/outputs/out1 \
    --imagedir /fs/ess/PAS2699/Demo_data/Weed_data \
    --grid-width 30 --grid-height 30 \
    --fov-width-ft 29 --fov-height-ft 22 \
    --gps-offset-x-ft 0 --gps-offset-y-ft 0 \
    --spray-mode binary \
    --score-threshold 0.3 \
    --class-filter weed