# ----------------------------------------------------------------------------
# -                        Open3D: www.open3d.org                            -
# ----------------------------------------------------------------------------
# The MIT License (MIT)
#
# Copyright (c) 2018-2021 www.open3d.org
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
# ----------------------------------------------------------------------------

# examples/python/reconstruction_system/slac_integrate.py

import numpy as np
import open3d as o3d
import open3d.core as o3c
import os, sys

pyexample_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(pyexample_path)

from utility.file import join, get_rgbd_file_lists

sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def run(config):
    print("slac non-rigid optimization.")
    o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Debug)

    # dataset path and slac subfolder path
    # slac default subfolder for 0.050 voxel size: `dataset/slac/0.050/`.
    path_dataset = config["path_dataset"]
    slac_folder = join(path_dataset, config["subfolder_slac"])

    # Read RGBD images.
    [color_files, depth_files] = get_rgbd_file_lists(config["path_dataset"])
    if len(color_files) != len(depth_files):
        raise ValueError(
            "The number of color images {} must equal to the number of depth images {}."
            .format(len(color_files), len(depth_files)))

    # Read optimized pose graph. [Generated by `register` stage].
    posegraph = o3d.io.read_pose_graph(
        join(slac_folder, config["template_optimized_posegraph_slac"]))

    # If camera intrinsic is not provided,
    # the default PrimeSense intrinsic is used.
    if config["path_intrinsic"]:
        intrinsic = o3d.io.read_pinhole_camera_intrinsic(
            config["path_intrinsic"])
    else:
        intrinsic = o3d.camera.PinholeCameraIntrinsic(
            o3d.camera.PinholeCameraIntrinsicParameters.PrimeSenseDefault)

    focal_length = intrinsic.get_focal_length()
    principal_point = intrinsic.get_principal_point()

    intrinsic_t = o3d.core.Tensor([[focal_length[0], 0, principal_point[0]],
                                   [0, focal_length[1], principal_point[1]],
                                   [0, 0, 1]])

    device = o3d.core.Device(
        'CUDA:0' if o3d.core.cuda.is_available() else 'CPU:0')
    voxel_grid = o3d.t.geometry.VoxelBlockGrid(
        attr_names=('tsdf', 'weight', 'color'),
        attr_dtypes=(o3c.float32, o3c.float32, o3c.float32),
        attr_channels=((1), (1), (3)),
        voxel_size=config['tsdf_cubic_size'] / 512,
        block_resolution=16,
        block_count=config['block_count'],
        device=o3d.core.Device('CUDA:0'))

    # Load control grid.
    ctr_grid_keys = o3d.core.Tensor.load(slac_folder + "ctr_grid_keys.npy")
    ctr_grid_values = o3d.core.Tensor.load(slac_folder + "ctr_grid_values.npy")

    ctr_grid = o3d.t.pipelines.slac.control_grid(3.0 / 8,
                                                 ctr_grid_keys.to(device),
                                                 ctr_grid_values.to(device),
                                                 device)

    fragment_folder = join(path_dataset, config["folder_fragment"])

    k = 0
    for i in range(len(posegraph.nodes)):
        fragment_pose_graph = o3d.io.read_pose_graph(
            join(fragment_folder, "fragment_optimized_%03d.json" % i))
        for node in fragment_pose_graph.nodes:
            pose_local = node.pose
            extrinsic_local_t = o3d.core.Tensor(np.linalg.inv(pose_local))

            pose = np.dot(posegraph.nodes[i].pose, node.pose)
            extrinsic_t = o3d.core.Tensor(np.linalg.inv(pose))

            depth = o3d.t.io.read_image(depth_files[k]).to(device)
            color = o3d.t.io.read_image(color_files[k]).to(device)
            rgbd = o3d.t.geometry.RGBDImage(color, depth)

            print('Deforming and integrating Frame {:3d}'.format(k))
            rgbd_projected = ctr_grid.deform(rgbd, intrinsic_t,
                                             extrinsic_local_t,
                                             config["depth_scale"],
                                             config["max_depth"])

            frustum_block_coords = voxel_grid.compute_unique_block_coordinates(
                rgbd_projected.depth, intrinsic_t, extrinsic_t,
                config['depth_scale'], config['max_depth'])

            voxel_grid.integrate(frustum_block_coords, rgbd_projected.depth,
                                 rgbd_projected.color, intrinsic_t, intrinsic_t, extrinsic_t,
                                 config["depth_scale"], config["max_depth"])
            k = k + 1

    if (config["save_output_as"] == "pointcloud"):
        pcd = voxel_grid.extract_point_cloud().to(o3d.core.Device("CPU:0"))
        save_pcd_path = join(slac_folder, "output_slac_pointcloud.ply")
        o3d.t.io.write_point_cloud(save_pcd_path, pcd)
    else:
        mesh = voxel_grid.extract_triangle_mesh().to(o3d.core.Device("CPU:0"))
        mesh_legacy = mesh.to_legacy()
        save_mesh_path = join(slac_folder, "output_slac_mesh.ply")
        o3d.io.write_triangle_mesh(save_mesh_path, mesh_legacy)
