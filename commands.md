# Old Command
python src/image_selection/sample_images_along_trajectory.py --interval-m 1.0 --db src/navigation/inspection_planner_test/databases/mtr_inspection.db --commit-gt --no-skip-existing-gt --no-copy --inspection 1

# New Command
python src/image_selection/sample_odometry_along_trajectory.py --db src/navigation/inspection_planner_test/databases/mtr_inspection_copy.db --inspection 1 --interval-m 
1.0